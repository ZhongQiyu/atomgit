# -*- coding: utf-8 -*-
"""A general dialog agent."""
from typing import Optional
import json
import uuid
import os
import pika
from loguru import logger
import threading
from ..message import Msg
from .agent import AgentBase
from ..prompt import PromptType
from OSSUtil import getBucketByPackagerepository, upload_file_2_oss, downloadOSSFile
from agentscope.web_ui.utils import generate_image_from_name, send_chat_msg

config = {
'MQ_PORT' : 5672,
'VIRTUAL_HOST' : '/',
'OSS_UPLOAD_IMAGE_DIR' : 'deco_upload',
}

class EditImageAgent(AgentBase):
    """A simple agent used to perform a dialogue. Your can set its role by
    `sys_prompt`."""

    def __init__(
        self,
        name: str,
        bucket: None,
        connection: None,
        channel: None,
        sys_prompt: str,
        model_config_name: str,
        use_memory: bool = True,
        memory_config: Optional[dict] = None,
        prompt_type: Optional[PromptType] = None,
    ) -> None:
        """Initialize the dialog agent.

        Arguments:
            name (`str`):
                The name of the agent.
            sys_prompt (`Optional[str]`):
                The system prompt of the agent, which can be passed by args
                or hard-coded in the agent.
            model_config_name (`str`):
                The name of the model config, which is used to load model from
                configuration.
            use_memory (`bool`, defaults to `True`):
                Whether the agent has memory.
            memory_config (`Optional[dict]`):
                The config of memory.
            prompt_type (`Optional[PromptType]`, defaults to
            `PromptType.LIST`):
                The type of the prompt organization, chosen from
                `PromptType.LIST` or `PromptType.STRING`.
        """
        super().__init__(
            name=name,
            sys_prompt=sys_prompt,
            model_config_name=model_config_name,
            use_memory=use_memory,
            memory_config=memory_config,
        )

        self.bucket = bucket
        self.connection = connection
        self.channel = channel
        result = self.channel.queue_declare(queue='', exclusive=True)
        self.callback_queue = result.method.queue

        self.channel.basic_consume(queue=self.callback_queue, on_message_callback=self.on_response, auto_ack=True)

        self.image_dir = 'local_images'

        if prompt_type is not None:
            logger.warning(
                "The argument `prompt_type` is deprecated and "
                "will be removed in the future.",
            )

    def reply(self, x: dict = None) -> dict:
        """Reply function of the agent. Processes the input data,
        generates a prompt using the current dialogue memory and system
        prompt, and invokes the language model to produce a response. The
        response is then formatted and added to the dialogue memory.

        Args:
            x (`dict`, defaults to `None`):
                A dictionary representing the user's input to the agent. This
                input is added to the dialogue memory if provided. Defaults to
                None.
        Returns:
            A dictionary representing the message generated by the agent in
            response to the user's input.
        """

        msg = Msg(self.name, x["user_input"], role="user")
        # record the input if needed
        if self.memory:
            self.memory.clear()
            self.memory.add(msg)

        # prepare prompt
        prompt = self.model.format(
            Msg("system", self.sys_prompt, role="system"),
            self.memory and self.memory.get_memory(),  # type: ignore[arg-type]
        )

        # call llm and generate response
        response = self.model(prompt).text
        converted_json = self.convert_to_json(response)

        self.corr_id = str(uuid.uuid4())
        image_oss_key = x["image_path"]
        message = {
            "bucket": "livedeco-test",
            "sourceUrl": image_oss_key,
            "packageId": self.corr_id,
            "request_type": "inpaint",
            "dino_text_prompt": converted_json['source_furniture'],
            "inpaint_prompt": converted_json['desired_furniture']
        }
        message = json.dumps(message)
        self.response = None
        self.channel.basic_publish(exchange=os.getenv('QUEUE_NAME'),
                                   routing_key=os.getenv('QUEUE_NAME'),
                                   properties=pika.BasicProperties(
                                       reply_to=self.callback_queue,
                                       correlation_id=self.corr_id,
                                   ),
                                   body=str(message))
        while self.response is None:
            self.connection.process_data_events()
        if self.response.decode() == 'failed':
            e = Exception()
            raise e
        else:
            current_img_dir = os.path.join(self.image_dir, self.corr_id)
            if not os.path.exists(current_img_dir):
                os.makedirs(current_img_dir)
            created_image_oss_key = 'AIGCs/' + self.corr_id + '/0.png'
            downloadOSSFile(self.bucket, created_image_oss_key, current_img_dir)

            msg = Msg(self.name, os.path.join(current_img_dir, '0.png'))
            self.memory.add(msg)
            self.speak(msg)

            # image_path = os.path.join(current_img_dir, '0.png')
            return created_image_oss_key

    def on_response(self, ch, method, props, body):
        if self.corr_id == props.correlation_id:
            self.response = body

    def convert_to_json(self, input_string):
        # 中英文标点符号的映射表
        punctuation_map = {
            "，": ",",
            "。": ".",
            "！": "!",
            "？": "?",
            "：": ":",
            "；": ";",
            "“": "\"",
            "”": "\"",
            "‘": "'",
            "’": "'",
            "（": "(",
            "）": ")",
            "【": "[",
            "】": "]",
            "《": "<",
            "》": ">",
            "、": ",",
            "—": "-",
            "…": "..."
            # 可以根据需要添加更多的符号
        }

        # 将中文符号替换为英文符号
        for chinese_punc, english_punc in punctuation_map.items():
            input_string = input_string.replace(chinese_punc, english_punc)

        # 将替换后的字符串转换为JSON对象
        try:
            json_object = json.loads(input_string)
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON: {e}")
            return None

        return json_object

    def speak(self, x):
        logger.chat(x)
        thread_name = threading.currentThread().name
        if thread_name != "MainThread":
            avatar = generate_image_from_name(self.name)
            # msg = f"""这是生成的视频
            # <video src="{x["content"]}"></video>"""
            msg = f"""这是编辑后的装修效果图
                                <img src="{x["content"]}" />
                                """
            send_chat_msg(msg, role=self.name,
                          uid=thread_name, avatar=avatar)