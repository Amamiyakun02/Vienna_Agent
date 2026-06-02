from openai import AsyncOpenAI
import json
import os
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from utils import build_instruction


MCP_URL = "https://agentmcp-service-579f62a8.fastapicloud.dev/mcp"

class AgentEngine:
    def __init__(self, persona_file="agents/kurisu.json"):
        self.gpt = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        with open(persona_file, "r", encoding="utf-8") as person:
            self._agent_personality = json.load(person)

        self._agent_instruction = build_instruction(self._agent_personality)

    async def chat(self, prompt: str, lang: str = "id"):
        lang_instruction = (
            "IMPORTANT: You MUST respond in English. All explanations, suggestions, and conversation MUST be in English."
            if lang == "en" else
            "IMPORTANT: Anda HARUS merespons dalam Bahasa Indonesia. Semua penjelasan, saran, dan percakapan HARUS dalam Bahasa Indonesia."
        )
        system_content = f"{self._agent_instruction}\n\n{lang_instruction}"

        # 1. Hubungkan ke MCP Server menggunakan Streamable HTTP secara langsung
        async with streamablehttp_client(MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                
                # Ambil daftar tools secara dinamis dari MCP server Anda
                mcp_tools_resp = await session.list_tools()
                
                # Format ke format deklarasi tool standar (type: function)
                tools = []
                for t in mcp_tools_resp.tools:
                    tools.append({
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.inputSchema
                        }
                    })
                
                messages = [
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": prompt}
                ]
                
                # 2. Jalankan Agentic Loop (Mendukung tool calling berulang dengan streaming)
                while True:
                    response = await self.gpt.chat.completions.create(
                        model="gpt-5.4-mini-2026-03-17",
                        messages=messages,
                        tools=tools if tools else None,
                        stream=True
                    )
                    
                    active_tool_calls = {}
                    async for chunk in response:
                        choice = chunk.choices[0]
                        delta = choice.delta
                        
                        # 1. Stream content to client
                        if hasattr(delta, "content") and delta.content:
                            yield f"data: {json.dumps({'text': delta.content})}\n\n"
                            
                        # 2. Accumulate tool call delta if present
                        if hasattr(delta, "tool_calls") and delta.tool_calls:
                            for tc in delta.tool_calls:
                                index = tc.index
                                if index not in active_tool_calls:
                                    active_tool_calls[index] = {
                                        "id": tc.id or "",
                                        "name": tc.function.name or "",
                                        "arguments": tc.function.arguments or ""
                                    }
                                else:
                                    if tc.id:
                                        active_tool_calls[index]["id"] = tc.id
                                    if tc.function.name:
                                        active_tool_calls[index]["name"] = tc.function.name
                                    if tc.function.arguments:
                                        active_tool_calls[index]["arguments"] += tc.function.arguments

                    # Jika tidak ada panggilan tool, loop selesai
                    if not active_tool_calls:
                        break
                        
                    # Format ke format standard tool_calls list
                    formatted_tool_calls = []
                    for idx, tc in sorted(active_tool_calls.items()):
                        formatted_tool_calls.append({
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"]
                            }
                        })
                        
                    # Tambahkan pesan asisten ke riwayat percakapan
                    messages.append({
                        "role": "assistant",
                        "tool_calls": formatted_tool_calls
                    })
                    
                    # Jalankan setiap tool secara berurutan
                    for tc in formatted_tool_calls:
                        tool_name = tc["function"]["name"]
                        args_str = tc["function"]["arguments"] or "{}"
                        
                        try:
                            tool_args = json.loads(args_str)
                        except Exception:
                            tool_args = {}
                            
                        print(f"[OPENAI MCP] Calling tool: '{tool_name}' with args: {tool_args}")
                        
                        try:
                            # Eksekusi tool secara dinamis di MCP server Anda
                            result = await session.call_tool(tool_name, tool_args)
                            
                            # Ekstrak konten teks dari hasil tool eksekusi
                            result_text = ""
                            if hasattr(result, "content") and result.content:
                                for part in result.content:
                                    if hasattr(part, "text") and part.text:
                                        result_text += part.text
                                    elif isinstance(part, dict) and part.get("type") == "text":
                                        result_text += part.get("text", "")
                                    else:
                                        result_text += str(part)
                            else:
                                result_text = str(result)
                        except Exception as e:
                            result_text = f"Error executing tool: {str(e)}"
                            
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "name": tool_name,
                            "content": result_text
                        })





class GeminiAgentEngine:
    def __init__(self, persona_file="agents/kurisu.json"):
        # Hubungkan ke Google Gemini API asli menggunakan kompatibilitas OpenAI
        self.gpt = AsyncOpenAI(
            api_key=os.getenv("GEMINI_API_KEY"),
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
        )
        
        with open(persona_file, "r", encoding="utf-8") as person:
            self._agent_personality = json.load(person)

        self._agent_instruction = build_instruction(self._agent_personality)

    async def chat(self, prompt: str, lang: str = "id"):
        lang_instruction = (
            "IMPORTANT: You MUST respond in English. All explanations, suggestions, and conversation MUST be in English."
            if lang == "en" else
            "IMPORTANT: Anda HARUS merespons dalam Bahasa Indonesia. Semua penjelasan, saran, dan percakapan HARUS dalam Bahasa Indonesia."
        )
        system_content = f"{self._agent_instruction}\n\n{lang_instruction}"

        # 1. Hubungkan ke MCP Server menggunakan Streamable HTTP secara langsung
        async with streamablehttp_client(MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                
                # Ambil daftar tools secara dinamis dari MCP server Anda
                mcp_tools_resp = await session.list_tools()
                
                # Format ke format deklarasi tool yang dimengerti oleh Gemini/OpenAI
                tools = []
                for t in mcp_tools_resp.tools:
                    tools.append({
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.inputSchema
                        }
                    })
                
                messages = [
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": prompt}
                ]
                
                # 2. Jalankan Agentic Loop (Mendukung tool calling berulang dengan streaming)
                while True:
                    response = await self.gpt.chat.completions.create(
                        model="gemini-3.5-flash",
                        messages=messages,
                        tools=tools if tools else None,
                        stream=True
                    )
                    
                    active_tool_calls = {}
                    async for chunk in response:
                        choice = chunk.choices[0]
                        delta = choice.delta
                        
                        # 1. Stream content to client
                        if hasattr(delta, "content") and delta.content:
                            yield f"data: {json.dumps({'text': delta.content})}\n\n"
                            
                        # 2. Accumulate tool call delta if present
                        if hasattr(delta, "tool_calls") and delta.tool_calls:
                            for tc in delta.tool_calls:
                                index = tc.index
                                if index not in active_tool_calls:
                                    active_tool_calls[index] = {
                                        "id": tc.id or "",
                                        "name": tc.function.name or "",
                                        "arguments": tc.function.arguments or ""
                                    }
                                else:
                                    if tc.id:
                                        active_tool_calls[index]["id"] = tc.id
                                    if tc.function.name:
                                        active_tool_calls[index]["name"] = tc.function.name
                                    if tc.function.arguments:
                                        active_tool_calls[index]["arguments"] += tc.function.arguments

                    # Jika tidak ada panggilan tool, loop selesai
                    if not active_tool_calls:
                        break
                        
                    # Format ke format standard tool_calls list
                    formatted_tool_calls = []
                    for idx, tc in sorted(active_tool_calls.items()):
                        formatted_tool_calls.append({
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"]
                            }
                        })
                        
                    # Tambahkan pesan asisten ke riwayat percakapan
                    messages.append({
                        "role": "assistant",
                        "tool_calls": formatted_tool_calls
                    })
                    
                    # Jalankan setiap tool secara berurutan
                    for tc in formatted_tool_calls:
                        tool_name = tc["function"]["name"]
                        args_str = tc["function"]["arguments"] or "{}"
                        
                        try:
                            tool_args = json.loads(args_str)
                        except Exception:
                            tool_args = {}
                            
                        print(f"[GEMINI MCP] Calling tool: '{tool_name}' with args: {tool_args}")
                        
                        try:
                            # Eksekusi tool secara dinamis di MCP server Anda
                            result = await session.call_tool(tool_name, tool_args)
                            
                            # Ekstrak konten teks dari hasil tool eksekusi
                            result_text = ""
                            if hasattr(result, "content") and result.content:
                                for part in result.content:
                                    if hasattr(part, "text") and part.text:
                                        result_text += part.text
                                    elif isinstance(part, dict) and part.get("type") == "text":
                                        result_text += part.get("text", "")
                                    else:
                                        result_text += str(part)
                            else:
                                result_text = str(result)
                        except Exception as e:
                            result_text = f"Error executing tool: {str(e)}"
                            
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "name": tool_name,
                            "content": result_text
                        })


            