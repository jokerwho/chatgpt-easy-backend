import argparse
import asyncio
import json
import os
import random
import sys
import readline

import requests
import tls_client
import websockets.client as websockets
from websockets.exceptions import ConnectionClosedError
from typing import Generator, Optional

from rich import print
from rich.console import Console

console = Console()
DELIMITER = "\x1e"

# Generate random IP between range 13.104.0.0/14
FORWARDED_IP = (
    f"13.{random.randint(104, 107)}.{random.randint(0, 255)}.{random.randint(0, 255)}"
)


HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/110.0.0.0 Safari/537.36 Edg/110.0.1587.41"
    ),
    "origin": "https://www.bing.com",
    "referer": "https://www.bing.com/",
    "sec-ch-ua": '"Chromium";v="110", "Not A(Brand";v="24", "Microsoft Edge";v="110"',
    "sec-ch-ua-platform": "Windows",
    "x-forwarded-for": FORWARDED_IP,
}


class NotAllowedToAccess(Exception):
    pass


def append_identifier(msg: dict) -> str:
    """
    Appends special character to end of message to identify end of message
    """
    # Convert dict to json string
    return json.dumps(msg) + DELIMITER


class ChatHubRequest:
    """
    Request object for ChatHub
    """

    def __init__(
        self,
        conversation_signature: str,
        client_id: str,
        conversation_id: str,
        invocation_id: int = 0,
    ) -> None:
        self.struct: dict = {}

        self.client_id: str = client_id
        self.conversation_id: str = conversation_id
        self.conversation_signature: str = conversation_signature
        self.invocation_id: int = invocation_id

    def update(
        self,
        prompt: str,
    ) -> None:
        """
        Updates request object
        """
        self.struct = {
            "arguments": [
                {
                    "source": "cib",
                    "optionsSets": [
                        "nlu_direct_response_filter",
                        "deepleo",
                        "enable_debug_commands",
                        "disable_emoji_spoken_text",
                        "responsible_ai_policy_235",
                        "enablemm",
                    ],
                    "isStartOfSession": self.invocation_id == 0,
                    "message": {
                        "author": "user",
                        "inputMethod": "Keyboard",
                        "text": prompt,
                        "messageType": "Chat",
                    },
                    "conversationSignature": self.conversation_signature,
                    "participant": {
                        "id": self.client_id,
                    },
                    "conversationId": self.conversation_id,
                },
            ],
            "invocationId": str(self.invocation_id),
            "target": "chat",
            "type": 4,
        }
        self.invocation_id += 1


class Conversation:
    """
    Conversation API
    """

    def __init__(self, cookiePath: str = "") -> None:
        self.struct: dict = {
            "conversationId": None,
            "clientId": None,
            "conversationSignature": None,
            "result": {"value": "Success", "message": None},
        }
        self.session = tls_client.Session(client_identifier="chrome_108")
        # POST request to get token
        # Create cookies
        if cookiePath == "":
            f = open(os.environ.get("COOKIE_FILE"), encoding="utf-8").read()
        else:
            f = open(cookiePath, encoding="utf8").read()
        cookie_file = json.loads(f)
        for cookie in cookie_file:
            self.session.cookies.set(cookie["name"], cookie["value"])
        url = "https://edgeservices.bing.com/edgesvc/turing/conversation/create"
        # Send GET request
        response = requests.get(
            url,
            timeout=30,
            headers=HEADERS,
            allow_redirects=True,
        )
        if response.status_code != 200:
            console.print(f"Status code: {response.status_code}")
            console.print(response.text)
            raise Exception("Authentication failed")
        try:
            self.struct = response.json()
            if self.struct["result"]["value"] == "UnauthorizedRequest":
                raise NotAllowedToAccess(self.struct["result"]["message"])
        except (json.decoder.JSONDecodeError, NotAllowedToAccess) as exc:
            raise Exception(
                "Authentication failed. You have not been accepted into the beta.",
            ) from exc


class ChatHub:
    """
    Chat API
    """

    def __init__(self, conversation: Conversation) -> None:
        self.wss: Optional[websockets.WebSocketClientProtocol] = None
        self.request: ChatHubRequest
        self.loop: bool
        self.task: asyncio.Task
        self.request = ChatHubRequest(
            conversation_signature=conversation.struct["conversationSignature"],
            client_id=conversation.struct["clientId"],
            conversation_id=conversation.struct["conversationId"],
        )

    async def ask_stream(self, prompt: str) -> Generator[str, None, None]:
        """
        Ask a question to the bot
        """
        # Check if websocket is closed
        if self.wss:
            if self.wss.closed:
                self.wss = await websockets.connect(
                    "wss://sydney.bing.com/sydney/ChatHub",
                    extra_headers=HEADERS,
                    max_size=None,
                )
                await self.__initial_handshake()
        else:
            self.wss = await websockets.connect(
                "wss://sydney.bing.com/sydney/ChatHub",
                extra_headers=HEADERS,
                max_size=None,
            )
            await self.__initial_handshake()
        # Construct a ChatHub request
        self.request.update(prompt=prompt)
        # Send request
        await self.wss.send(append_identifier(self.request.struct))
        final = False
        while not final:
            objects = str(await self.wss.recv()).split(DELIMITER)
            for obj in objects:
                if obj is None or obj == "":
                    continue
                response = json.loads(obj)
                if response.get("type") == 1:
                    yield False, response["arguments"][0]["messages"][0][
                        "adaptiveCards"
                    ][0]["body"][0]["text"]
                elif response.get("type") == 2:
                    final = True
                    yield True, response

    async def __initial_handshake(self):
        await self.wss.send(append_identifier({"protocol": "json", "version": 1}))
        await self.wss.recv()

    async def close(self):
        """
        Close the connection
        """
        if self.wss:
            if not self.wss.closed:
                await self.wss.close()


class Chatbot:
    """
    Combines everything to make it seamless
    """

    def __init__(self, cookiePath: str = "") -> None:
        self.chat_hub: ChatHub = ChatHub(Conversation(cookiePath))

    async def ask(self, prompt: str) -> dict:
        """
        Ask a question to the bot
        """
        async for final, response in self.chat_hub.ask_stream(prompt=prompt):
            if final:
                return response

    async def ask_stream(self, prompt: str) -> Generator[str, None, None]:
        """
        Ask a question to the bot
        """
        async for response in self.chat_hub.ask_stream(prompt=prompt):
            yield response

    async def close(self):
        """
        Close the connection
        """
        await self.chat_hub.close()

    async def reset(self):
        """
        Reset the conversation
        """
        await self.close()
        self.chat_hub = ChatHub(Conversation())


def get_input(prompt):
    """
    Multi-line input function
    """
    # Display the prompt
    console.print(prompt, end="", style="bold green")

    # Initialize an empty list to store the input lines
    lines = []

    # Read lines of input until the user enters an empty line
    while True:
        line = input()
        if line == "":
            break
        lines.append(line)

    # Join the lines, separated by newlines, and store the result
    user_input = "\n".join(lines)

    # Return the input
    return user_input


async def main():
    """
    Main function
    """
    with console.status("[bold green]?????????Bot???...") as status:
        bot = Chatbot()
    while True:
        try:
            prompt = get_input("\n???:\n")
        except KeyboardInterrupt:
            console.print("\n?????????...", style="bold green")
            break
        if prompt == "!exit":
            break
        elif prompt == "!help":
            console.print(
                """
            !help - ??????????????????
            !exit - ????????????
            !reset - ????????????
            """,
            )
            continue
        elif prompt == "!reset":
            await bot.reset()
            console.print("??????????????????", style="bold red")
            continue
        console.print("\nBot:", style="bold cyan")
        if args.no_stream:
            console.print(
                (await bot.ask(prompt=prompt))["item"]["messages"][1]["adaptiveCards"][
                    0
                ]["body"][0]["text"],
            )
        else:
            wrote = 0
            try:
                thinking = console.status("[bold yellow]?????????...")
                thinking.start()
                answer = ""
                async for final, response in bot.ask_stream(prompt=prompt):
                    if not final:
                        thinking.stop()
                        answer += response[wrote:]
                        console.print(response[wrote:], end="")
                        wrote = len(response)
                        sys.stdout.flush()
                    else:
                        thinking.stop()
                        if not answer:
                            console.print("??????,???????????????", style="bold red")
            except ConnectionClosedError:
                thinking.stop()
                console.print("????????????????????????????????????????????????")
                with console.status("[bold green]?????????Bot???...") as status:
                    bot = Chatbot()
            thinking.stop()
            # console.print()
        sys.stdout.flush()
    await bot.close()


if __name__ == "__main__":
    console.print(
        """
        [bold cyan]EdgeGPT - Bing GPT???????????????[/bold cyan]
        ?????? [u]!help[/u] ????????????
        ?????? [u]!exit[/u] ????????????
        [bold red]??????????????????,?????????????????????????????????[/bold red]
    """,
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-stream", action="store_true")
    parser.add_argument(
        "--cookie-file", type=str, default="cookies.json", required=True
    )
    args = parser.parse_args()
    os.environ["COOKIE_FILE"] = args.cookie_file
    args = parser.parse_args()
    asyncio.run(main())
