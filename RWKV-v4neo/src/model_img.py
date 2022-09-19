########################################################################################################
# The RWKV Language Model - https://github.com/BlinkDL/RWKV-LM
########################################################################################################

import numpy as np
import os
import torch
from torchvision import models
import torch.nn as nn
import torch.nn.functional as F


class L2pooling(nn.Module):
    def __init__(self, filter_size=5, stride=2, channels=None, pad_off=0):
        super(L2pooling, self).__init__()
        self.padding = (filter_size - 2) // 2
        self.stride = stride
        self.channels = channels
        a = np.hanning(filter_size)[1:-1]
        g = torch.Tensor(a[:, None] * a[None, :])
        g = g / torch.sum(g)
        self.register_buffer(
            "filter", g[None, None, :, :].repeat((self.channels, 1, 1, 1))
        )

    def forward(self, input):
        input = input**2
        out = F.conv2d(
            input,
            self.filter,
            stride=self.stride,
            padding=self.padding,
            groups=input.shape[1],
        )
        return (out + 1e-12).sqrt()


class DISTS(torch.nn.Module):
    def __init__(self, load_weights=True):
        super(DISTS, self).__init__()
        vgg_pretrained_features = models.vgg16(
            weights="VGG16_Weights.IMAGENET1K_V1"
        ).features
        self.stage1 = torch.nn.Sequential()
        self.stage2 = torch.nn.Sequential()
        self.stage3 = torch.nn.Sequential()
        self.stage4 = torch.nn.Sequential()
        self.stage5 = torch.nn.Sequential()
        for x in range(0, 4):
            self.stage1.add_module(str(x), vgg_pretrained_features[x])
        self.stage2.add_module(str(4), L2pooling(channels=64))
        for x in range(5, 9):
            self.stage2.add_module(str(x), vgg_pretrained_features[x])
        self.stage3.add_module(str(9), L2pooling(channels=128))
        for x in range(10, 16):
            self.stage3.add_module(str(x), vgg_pretrained_features[x])
        self.stage4.add_module(str(16), L2pooling(channels=256))
        for x in range(17, 23):
            self.stage4.add_module(str(x), vgg_pretrained_features[x])
        self.stage5.add_module(str(23), L2pooling(channels=512))
        for x in range(24, 30):
            self.stage5.add_module(str(x), vgg_pretrained_features[x])

        self.register_buffer(
            "mean", torch.tensor([0.485, 0.456, 0.406]).view(1, -1, 1, 1)
        )
        self.register_buffer(
            "std", torch.tensor([0.229, 0.224, 0.225]).view(1, -1, 1, 1)
        )

        self.chns = [3, 64, 128, 256, 512, 512]
        self.register_buffer(
            "alpha", nn.Parameter(torch.randn(1, sum(self.chns), 1, 1))
        )
        self.register_buffer("beta", nn.Parameter(torch.randn(1, sum(self.chns), 1, 1)))
        self.alpha.data.normal_(0.1, 0.01)
        self.beta.data.normal_(0.1, 0.01)
        weights = torch.load("test/DISTS_weights.pt")
        self.alpha.data = weights["alpha"]
        self.beta.data = weights["beta"]

        for param in self.parameters():
            param.requires_grad = False

    def forward_once(self, x):
        h = (x - self.mean) / self.std
        h = self.stage1(h)
        h_relu1_2 = h
        h = self.stage2(h)
        h_relu2_2 = h
        h = self.stage3(h)
        h_relu3_3 = h
        h = self.stage4(h)
        h_relu4_3 = h
        h = self.stage5(h)
        h_relu5_3 = h
        return [x, h_relu1_2, h_relu2_2, h_relu3_3, h_relu4_3, h_relu5_3]

    def forward(self, x, y, require_grad=False, batch_average=False):
        if require_grad:
            feats0 = self.forward_once(x)
            feats1 = self.forward_once(y)
        else:
            with torch.no_grad():
                feats0 = self.forward_once(x)
                feats1 = self.forward_once(y)
        dist1 = 0
        dist2 = 0
        c1 = 1e-6
        c2 = 1e-6
        w_sum = self.alpha.sum() + self.beta.sum()
        alpha = torch.split(self.alpha / w_sum, self.chns, dim=1)
        beta = torch.split(self.beta / w_sum, self.chns, dim=1)

        for k in range(len(self.chns)):
            x_mean = feats0[k].mean([2, 3], keepdim=True)
            y_mean = feats1[k].mean([2, 3], keepdim=True)
            S1 = (2 * x_mean * y_mean + c1) / (x_mean**2 + y_mean**2 + c1)
            dist1 = dist1 + (alpha[k] * S1).sum(1, keepdim=True)

            x_var = ((feats0[k] - x_mean) ** 2).mean([2, 3], keepdim=True)
            y_var = ((feats1[k] - y_mean) ** 2).mean([2, 3], keepdim=True)
            xy_cov = (feats0[k] * feats1[k]).mean(
                [2, 3], keepdim=True
            ) - x_mean * y_mean
            S2 = (2 * xy_cov + c2) / (x_var + y_var + c2)
            dist2 = dist2 + (beta[k] * S2).sum(1, keepdim=True)

        score = 1 - (dist1 + dist2).squeeze()

        if batch_average:
            return score.mean()
        else:
            return score


import os, math, gc
import torchvision as vision
import torch
import torch.nn as nn
from torch.nn import functional as F
import pytorch_lightning as pl
from pytorch_lightning.utilities import rank_zero_info, rank_zero_only
from pytorch_lightning.strategies import DeepSpeedStrategy
import deepspeed
from deepspeed.ops.adam import DeepSpeedCPUAdam, FusedAdam


class ToBinary(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x):
        return torch.floor(x + torch.empty_like(x).uniform_(0.4, 0.6))

    @staticmethod
    def backward(ctx, grad_output):
        return grad_output.clone()


MyModule = torch.jit.ScriptModule
MyFunction = torch.jit.script_method


class R_ENCODER(MyModule):
    def __init__(self, args):
        super().__init__()
        self.args = args

        self.B00 = nn.BatchNorm2d(12)
        self.C00 = nn.Conv2d(12, 96, kernel_size=3, padding=1)
        self.C01 = nn.Conv2d(96, 12, kernel_size=3, padding=1)
        self.C02 = nn.Conv2d(12, 96, kernel_size=3, padding=1)
        self.C03 = nn.Conv2d(96, 12, kernel_size=3, padding=1)

        self.B10 = nn.BatchNorm2d(48)
        self.C10 = nn.Conv2d(48, 192, kernel_size=3, padding=1)
        self.C11 = nn.Conv2d(192, 48, kernel_size=3, padding=1)
        self.C12 = nn.Conv2d(48, 192, kernel_size=3, padding=1)
        self.C13 = nn.Conv2d(192, 48, kernel_size=3, padding=1)

        self.B20 = nn.BatchNorm2d(192)
        self.C20 = nn.Conv2d(192, 192, kernel_size=3, padding=1)
        self.C21 = nn.Conv2d(192, 192, kernel_size=3, padding=1)
        self.C22 = nn.Conv2d(192, 192, kernel_size=3, padding=1)
        self.C23 = nn.Conv2d(192, 192, kernel_size=3, padding=1)

        self.COUT = nn.Conv2d(192, 8, kernel_size=3, padding=1)

    @MyFunction
    def forward(self, x):
        ACT = F.silu

        x = F.pixel_unshuffle(x, 2)
        x = x + self.C01(ACT(self.C00(ACT(self.B00(x)))))
        x = x + self.C03(ACT(self.C02(x)))

        x = F.pixel_unshuffle(x, 2)
        x = x + self.C11(ACT(self.C10(ACT(self.B10(x)))))
        x = x + self.C13(ACT(self.C12(x)))

        x = F.pixel_unshuffle(x, 2)
        x = x + self.C21(ACT(self.C20(ACT(self.B20(x)))))
        x = x + self.C23(ACT(self.C22(x)))

        x = self.COUT(x)

        return torch.sigmoid(x)


class R_DECODER(MyModule):
    def __init__(self, args):
        super().__init__()
        self.args = args

        self.CIN = nn.Conv2d(8, 192, kernel_size=3, padding=1)

        self.B00 = nn.BatchNorm2d(192)
        self.C00 = nn.Conv2d(192, 192, kernel_size=3, padding=1)
        self.C01 = nn.Conv2d(192, 192, kernel_size=3, padding=1)
        self.C02 = nn.Conv2d(192, 192, kernel_size=3, padding=1)
        self.C03 = nn.Conv2d(192, 192, kernel_size=3, padding=1)

        self.B10 = nn.BatchNorm2d(48)
        self.C10 = nn.Conv2d(48, 192, kernel_size=3, padding=1)
        self.C11 = nn.Conv2d(192, 48, kernel_size=3, padding=1)
        self.C12 = nn.Conv2d(48, 192, kernel_size=3, padding=1)
        self.C13 = nn.Conv2d(192, 48, kernel_size=3, padding=1)

        self.B20 = nn.BatchNorm2d(12)
        self.C20 = nn.Conv2d(12, 96, kernel_size=3, padding=1)
        self.C21 = nn.Conv2d(96, 12, kernel_size=3, padding=1)
        self.C22 = nn.Conv2d(12, 96, kernel_size=3, padding=1)
        self.C23 = nn.Conv2d(96, 12, kernel_size=3, padding=1)

    @MyFunction
    def forward(self, x):
        ACT = F.silu

        x = self.CIN(x)

        x = x + self.C01(ACT(self.C00(ACT(self.B00(x)))))
        x = x + self.C03(ACT(self.C02(x)))
        x = F.pixel_shuffle(x, 2)

        x = x + self.C11(ACT(self.C10(ACT(self.B10(x)))))
        x = x + self.C13(ACT(self.C12(x)))
        x = F.pixel_shuffle(x, 2)

        x = x + self.C21(ACT(self.C20(ACT(self.B20(x)))))
        x = x + self.C23(ACT(self.C22(x)))
        x = F.pixel_shuffle(x, 2)

        return torch.sigmoid(x)


class RWKV_IMG(pl.LightningModule):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.encoder = R_ENCODER(args)
        self.decoder = R_DECODER(args)
        self.loss_dists = DISTS()

    def configure_optimizers(self):
        args = self.args
        optim_groups = [
            {"params": [p for n, p in self.named_parameters()], "weight_decay": 0.0},
        ]
        if self.deepspeed_offload:
            return DeepSpeedCPUAdam(
                optim_groups,
                lr=self.args.lr_init,
                betas=self.args.betas,
                eps=self.args.adam_eps,
                bias_correction=True,
                adamw_mode=False,
                weight_decay=0,
                amsgrad=False,
            )
        return FusedAdam(
            optim_groups,
            lr=self.args.lr_init,
            betas=self.args.betas,
            eps=self.args.adam_eps,
            bias_correction=True,
            adam_w_mode=False,
            weight_decay=0,
            amsgrad=False,
        )
        # return ZeroOneAdam(optim_groups, lr=self.args.lr_init, betas=self.args.betas, eps=self.args.adam_eps, bias_correction=True, weight_decay=0, amsgrad=False, cuda_aware=False)

    @property
    def deepspeed_offload(self) -> bool:
        strategy = self.trainer.strategy
        if isinstance(strategy, DeepSpeedStrategy):
            config = strategy.config["zero_optimization"]
            return config.get("offload_optimizer") or config.get("offload_param")
        return False

    def forward(self, img):
        z = self.encoder(img)
        z = ToBinary.apply(z)
        out = self.decoder(z)
        return out

    def training_step(self, batch, batch_idx):
        args = self.args
        img, txt = batch
        out = self(img)
        if self.trainer.is_global_zero:
            if (self.trainer.global_step + 1) % (100 * int(args.devices)) == 0:
                vision.utils.save_image(
                    img[:4], f"test/image_model/{self.trainer.global_step}-src.jpg"
                )
                vision.utils.save_image(
                    out[:4], f"test/image_model/{self.trainer.global_step}-out.jpg"
                )

        loss_l1 = F.l1_loss(out, img)
        loss_dists = self.loss_dists(out, img, require_grad=True, batch_average=True)

        return loss_l1 + loss_dists

    def training_step_end(self, batch_parts):
        all = self.all_gather(batch_parts)
        if self.trainer.is_global_zero:
            self.trainer.my_loss_all = all

    def generate_init_weight(self):
        print(
            f"""
############################################################################
#
# Init model weight (slow for large models)...
#
############################################################################
"""
        )
        m = {}
        for n in self.state_dict():
            p = self.state_dict()[n]
            shape = p.shape

            m[n] = p

            m[n] = m[n].cpu()
            if os.environ["RWKV_FLOAT_MODE"] == "fp16":
                m[n] = m[n].half()
            elif os.environ["RWKV_FLOAT_MODE"] == "bf16":
                m[n] = m[n].bfloat16()

        gc.collect()
        torch.cuda.empty_cache()
        return m
