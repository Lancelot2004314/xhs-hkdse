# https://github.com/modelcontextprotocol/python-sdk/blob/main/examples/clients/simple-chatbot/mcp_simple_chatbot/main.py
import asyncio
import json
import logging
import os
import shutil
from contextlib import AsyncExitStack
from typing import Any

import openai
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class Configuration:
    """Manages configuration and environment variables for the MCP client."""

    def __init__(self) -> None:
        """Initialize configuration with environment variables."""
        self.load_env()
        self.api_key = os.getenv("LLM_API_KEY")
        self.base_url = os.getenv("OPENAI_BASE_URL")
        self.default_model = os.getenv("DEFAULT_MODEL", "claude-sonnet-4-20250514")

    @staticmethod
    def load_env() -> None:
        """Load environment variables from .env file."""
        load_dotenv()

    @staticmethod
    def load_config(file_path: str) -> dict[str, Any]:
        """Load server configuration from JSON file.

        Args:
            file_path: Path to the JSON configuration file.

        Returns:
            Dict containing server configuration.

        Raises:
            FileNotFoundError: If configuration file doesn't exist.
            JSONDecodeError: If configuration file is invalid JSON.
        """
        with open(file_path, "r") as f:
            return json.load(f)

    @property
    def llm_api_key(self) -> str:
        """Get the LLM API key.

        Returns:
            The API key as a string.

        Raises:
            ValueError: If the API key is not found in environment variables.
        """
        if not self.api_key:
            raise ValueError("LLM_API_KEY not found in environment variables")
        return self.api_key

    @property
    def openai_base_url(self) -> str:
        """Get the OpenAI base URL.

        Returns:
            The base URL as a string.

        Raises:
            ValueError: If the base URL is not found in environment variables.
        """
        if not self.base_url:
            raise ValueError("OPENAI_BASE_URL not found in environment variables")
        return self.base_url


class Server:
    """Manages MCP server connections and tool execution."""

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name: str = name
        self.config: dict[str, Any] = config
        self.stdio_context: Any | None = None
        self.session: ClientSession | None = None
        self._cleanup_lock: asyncio.Lock = asyncio.Lock()
        self.exit_stack: AsyncExitStack = AsyncExitStack()

    async def initialize(self) -> None:
        """Initialize the server connection."""
        server_type = self.config.get("type", "stdio")
        
        if server_type == "streamable_http":
            # Handle streamable_http type
            url = self.config.get("url")
            if not url:
                raise ValueError(f"URL is required for streamable_http server {self.name}")
            
            try:
                # 使用 AsyncExitStack 管理连接生命周期
                transport = await self.exit_stack.enter_async_context(streamablehttp_client(url))
                read, write, _ = transport
                session = await self.exit_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self.session = session
            except Exception as e:
                logging.error(f"Error initializing streamable_http server {self.name}: {e}")
                await self.cleanup()
                raise
        else:
            # Handle stdio type (default)
            command = shutil.which("npx") if self.config["command"] == "npx" else self.config["command"]
            if command is None:
                raise ValueError("The command must be a valid string and cannot be None.")

            server_params = StdioServerParameters(
                command=command,
                args=self.config["args"],
                env={**os.environ, **self.config["env"]} if self.config.get("env") else None,
            )
            try:
                stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
                read, write = stdio_transport
                session = await self.exit_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self.session = session
            except Exception as e:
                logging.error(f"Error initializing server {self.name}: {e}")
                await self.cleanup()
                raise

    async def list_tools(self) -> list[Any]:
        """List available tools from the server.

        Returns:
            A list of available tools.

        Raises:
            RuntimeError: If the server is not initialized.
        """
        if not self.session:
            raise RuntimeError(f"Server {self.name} not initialized")

        tools_response = await self.session.list_tools()
        tools = []

        for item in tools_response:
            if isinstance(item, tuple) and item[0] == "tools":
                tools.extend(Tool(tool.name, tool.description, tool.inputSchema, tool.title) for tool in item[1])

        return tools

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        retries: int = 2,
        delay: float = 1.0,
    ) -> Any:
        """Execute a tool with retry mechanism.

        Args:
            tool_name: Name of the tool to execute.
            arguments: Tool arguments.
            retries: Number of retry attempts.
            delay: Delay between retries in seconds.

        Returns:
            Tool execution result.

        Raises:
            RuntimeError: If server is not initialized.
            Exception: If tool execution fails after all retries.
        """
        if not self.session:
            raise RuntimeError(f"Server {self.name} not initialized")

        attempt = 0
        while attempt < retries:
            try:
                logging.info(f"Executing {tool_name}...")
                result = await self.session.call_tool(tool_name, arguments)

                return result

            except Exception as e:
                attempt += 1
                logging.warning(f"Error executing tool: {e}. Attempt {attempt} of {retries}.")
                if attempt < retries:
                    logging.info(f"Retrying in {delay} seconds...")
                    await asyncio.sleep(delay)
                else:
                    logging.error("Max retries reached. Failing.")
                    raise

    async def cleanup(self) -> None:
        """Clean up server resources."""
        async with self._cleanup_lock:
            try:
                await self.exit_stack.aclose()
                self.session = None
                self.stdio_context = None
            except Exception as e:
                # 检查是否是 AsyncExitStack 的上下文错误（这是无害的）
                error_msg = str(e).lower()
                if "cancel scope" in error_msg or "different task" in error_msg:
                    # 这些错误是无害的，降级为 debug 日志
                    logging.debug(f"Cleanup context info for server {self.name}: {e}")
                else:
                    # 其他真正的错误才打印 error
                    logging.error(f"Error during cleanup of server {self.name}: {e}")


class Tool:
    """Represents a tool with its properties and formatting."""

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        title: str | None = None,
    ) -> None:
        self.name: str = name
        self.title: str | None = title
        self.description: str = description
        self.input_schema: dict[str, Any] = input_schema

    def format_for_llm(self) -> str:
        """Format tool information for LLM.

        Returns:
            A formatted string describing the tool.
        """
        args_desc = []
        if "properties" in self.input_schema:
            for param_name, param_info in self.input_schema["properties"].items():
                arg_desc = f"- {param_name}: {param_info.get('description', 'No description')}"
                if param_name in self.input_schema.get("required", []):
                    arg_desc += " (required)"
                args_desc.append(arg_desc)

        # Build the formatted output with title as a separate field
        output = f"Tool: {self.name}\n"

        # Add human-readable title if available
        if self.title:
            output += f"User-readable title: {self.title}\n"

        output += f"""Description: {self.description}
            Arguments:
            {chr(10).join(args_desc)}
            """

        return output

    def to_openai_tool(self) -> dict:
        """Convert tool to OpenAI function calling format.
        
        Returns:
            A dictionary in OpenAI tool format.
        """
        # 如果type是object但properties为空，添加空的properties
        parameters = self.input_schema.copy()
        if (parameters.get("type") == "object" and 
            ("properties" not in parameters or not parameters["properties"])):
            parameters["properties"] = {}
            
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters
            }
        }


class LLMClient:
    """Manages communication with the LLM provider."""

    def __init__(self, api_key: str, base_url: str, default_model: str = "claude-sonnet-4-20250514") -> None:
        self.client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url
        )
        self.default_model = default_model

    def get_tool_call_response(self, messages: list[dict[str, str]], tools: list = None, max_tokens: int = 32000):
        """Get a response from the LLM.

        Args:
            messages: A list of message dictionaries.
            tools: List of tools for function calling.

        Returns:
            The full response object from OpenAI.

        Raises:
            Exception: If the request to the LLM fails.
        """
        try:
            response = self.client.chat.completions.create(
                model=self.default_model,
                messages=messages,
                tools=tools,
                tool_choice="auto" if tools else None,
                temperature=0.8,
                # max_tokens=max_tokens
            )
            return response

        except Exception as e:
            error_message = f"Error getting LLM response: {str(e)}"
            logging.error(error_message)
            # Return a mock response object for error cases
            class ErrorResponse:
                def __init__(self, error_msg):
                    self.choices = [type('obj', (object,), {
                        'message': type('obj', (object,), {
                            'content': error_msg,
                            'tool_calls': None
                        })()
                    })()]
            return ErrorResponse(f"I encountered an error: {error_message}. Please try again or rephrase your request.")

    def get_final_response(self, messages: list[dict[str, str]], tools: list = None, max_tokens: int = 32000):
        """Get a final response that summarizes tool call results and answers the original question.

        Args:
            messages: A list of message dictionaries including tool results.
            tools: List of tools for function calling (usually None for final response).
            max_tokens: Maximum tokens for the response.

        Returns:
            The full response object from OpenAI with summarized content.

        Raises:
            Exception: If the request to the LLM fails.
        """
        try:
            # Add a system message to guide the final response generation
            enhanced_messages = messages.copy()
            
            # Find the original user question from the messages
            original_question = None
            for msg in messages:
                if msg.get("role") == "user":
                    original_question = msg.get("content")
                    break
            
            # Add guidance for final response with decision making
            summary_prompt = f"""
                Based on the tool execution results above, you need to decide whether to continue with more tool calls or provide a final summary for the original question: "{original_question}"
    
                Please analyze the current information and decide:
    
                OPTION 1 - If you need more information to complete the task:
                - Call additional tools to gather missing information
                - Use tools for deeper research or verification
                - Continue the investigation process
    
                OPTION 2 - If you have sufficient information（This is the main task）:
                - Provide a comprehensive and well-structured final response
                - Synthesize all tool results into a coherent answer
                - Organize the information logically and make it actionable
                - Include all relevant details from the tool outputs
                - Use a professional and helpful tone
    
                Make your decision based on whether the current information is sufficient to fully answer the original question and complete the task requirements.
            """
            
            enhanced_messages.append({
                "role": "system", 
                "content": summary_prompt
            })
            
            response = self.client.chat.completions.create(
                model=self.default_model,
                messages=enhanced_messages,
                tools=tools,  # Allow tools for continuation if needed
                tool_choice="auto" if tools else None,
                temperature=0.3,  # Lower temperature for more focused response
                # max_tokens=max_tokens
            )
            return response

        except Exception as e:
            error_message = f"Error getting final LLM response: {str(e)}"
            logging.error(error_message)
            # Return a mock response object for error cases
            class ErrorResponse:
                def __init__(self, error_msg):
                    self.choices = [type('obj', (object,), {
                        'message': type('obj', (object,), {
                            'content': error_msg,
                            'tool_calls': None
                        })()
                    })()]
            return ErrorResponse(f"I encountered an error while generating the final response: {error_message}. Please try again.")
        self.servers: list[Server] = servers
        self.llm_client: LLMClient = llm_client

    async def cleanup_servers(self) -> None:
        """Clean up all servers properly."""
        for server in reversed(self.servers):
            try:
                await server.cleanup()
            except Exception as e:
                logging.warning(f"Warning during final cleanup: {e}")

    async def process_llm_response(self, response) -> tuple[str, bool]:
        """Process the LLM response and execute tools if needed.

        Args:
            response: The full response object from OpenAI.

        Returns:
            Tuple of (content, has_tool_calls) where content is the response text
            and has_tool_calls indicates if tools were executed.
        """
        import json
        
        message = response.choices[0].message
        
        # Check if there are tool calls
        if message.tool_calls:
            tool_results = []
            for tool_call in message.tool_calls:
                tool_name = tool_call.function.name
                try:
                    arguments = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    arguments = {}
                
                logging.info(f"Executing tool: {tool_name}")
                logging.info(f"With arguments: {arguments}")

                # Find the appropriate server and execute the tool
                tool_executed = False
                for server in self.servers:
                    tools = await server.list_tools()
                    if any(tool.name == tool_name for tool in tools):
                        try:
                            result = await server.execute_tool(tool_name, arguments)

                            if isinstance(result, dict) and "progress" in result:
                                progress = result["progress"]
                                total = result["total"]
                                percentage = (progress / total) * 100
                                logging.info(f"Progress: {progress}/{total} ({percentage:.1f}%)")

                            tool_results.append(f"Tool {tool_name} result: {result}")
                            tool_executed = True
                            break
                        except Exception as e:
                            error_msg = f"Error executing tool {tool_name}: {str(e)}"
                            logging.error(error_msg)
                            tool_results.append(error_msg)
                            tool_executed = True
                            break

                if not tool_executed:
                    tool_results.append(f"No server found with tool: {tool_name}")

            return "\n".join(tool_results), True
        else:
            # No tool calls, return the content directly
            return message.content or "", False

    async def start(self) -> None:
        """Main chat session handler."""
        try:
            for server in self.servers:
                try:
                    await server.initialize()
                except Exception as e:
                    logging.error(f"Failed to initialize server: {e}")
                    await self.cleanup_servers()
                    return

            all_tools = []
            for server in self.servers:
                tools = await server.list_tools()
                all_tools.extend(tools)
                print(tools)

            openai_tools = [tool.to_openai_tool() for tool in all_tools] if all_tools else None
            print("available openai_tools:", [tool["function"]["name"] for tool in openai_tools])
            system_message = (
                "You are a helpful assistant with access to various tools. "
                "Use the appropriate tool based on the user's question. "
                "If no tool is needed, reply directly with a helpful response."
            )

            messages = [{"role": "system", "content": system_message}]

            while True:
                try:
                    user_input = "搜索当前机器学习最新研究趋势" #input("You: ").strip().lower()
                    if user_input in ["quit", "exit"]:
                        logging.info("\nExiting...")
                        break

                    messages.append({"role": "user", "content": user_input})

                    response = self.llm_client.get_tool_call_response(messages, openai_tools, max_tokens=8192)
                    result, has_tool_calls = await self.process_llm_response(response)

                    if has_tool_calls:
                        # Add the assistant's message with tool calls
                        assistant_message = response.choices[0].message
                        
                        # Create proper assistant message with tool calls
                        assistant_msg = {"role": "assistant", "content": assistant_message.content or ""}
                        if assistant_message.tool_calls:
                            assistant_msg["tool_calls"] = [
                                {
                                    "id": tool_call.id,
                                    "type": "function",
                                    "function": {
                                        "name": tool_call.function.name,
                                        "arguments": tool_call.function.arguments
                                    }
                                }
                                for tool_call in assistant_message.tool_calls
                            ]
                        messages.append(assistant_msg)
                        
                        # Add tool results as tool messages (not system messages)
                        if assistant_message.tool_calls:
                            for i, tool_call in enumerate(assistant_message.tool_calls):
                                tool_result_parts = result.split('\n')
                                tool_result = tool_result_parts[i] if i < len(tool_result_parts) else result
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "content": tool_result
                                })
                        
                        logging.info("\nTool execution results: %s", result)

                        # Get final response after tool execution
                        final_response = self.llm_client.get_final_response(messages, openai_tools)
                        final_content = final_response.choices[0].message.content or ""
                        logging.info("\nAssistant: %s", final_content)
                        messages.append({"role": "assistant", "content": final_content})
                    else:
                        # No tool calls, just add the response content
                        logging.info("\nAssistant: %s", result)
                        messages.append({"role": "assistant", "content": result or ""})

                except KeyboardInterrupt:
                    logging.info("\nExiting...")
                    break

        finally:
            await self.cleanup_servers()


async def main() -> None:
    """Initialize and run the chat session."""
    config = Configuration()
    # Get the directory of the current script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "servers_config.json")
    server_config = config.load_config(config_path)
    servers = [Server(name, srv_config) for name, srv_config in server_config["mcpServers"].items()]
    llm_client = LLMClient(config.llm_api_key, config.openai_base_url, config.default_model)
    chat_session = ChatSession(servers, llm_client)
    await chat_session.start()


if __name__ == "__main__":
    asyncio.run(main())