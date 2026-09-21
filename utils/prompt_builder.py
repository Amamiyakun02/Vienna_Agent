import json
from typing import List, Dict, Optional, Union


def _join_field(value: Union[str, list, None], fallback: str = "") -> str:
    """Safely join a field that may be a string or a list."""
    if value is None:
        return fallback
    if isinstance(value, list):
        return ", ".join(value)
    return str(value)


def build_instruction(data: dict) -> str:
    """
    Build a structured system prompt string from an agent personality config dict.
    Supports both Lina-style (consulting_style) and Kurisu-style (coding_style, knowledge_focus) schemas.
    """
    parts = [
        f"Assistant ID: {data.get('assistant_id', '')}",
        f"Name: {data.get('name', '')}",
        f"Role: {data.get('role', '')}",
    ]

    # --- About Author (profile of the creator) ---
    about = data.get("about_author", {})
    if about:
        parts.append("\n[About Author — Informasi Pencipta]")
        parts.append(f"  Name: {about.get('name', '')}")
        parts.append(f"  Role/Title: {about.get('role', '')} ({about.get('title', '')})")
        parts.append(f"  Bio: {about.get('bio', '')}")
        parts.append(f"  Philosophy: {about.get('philosophy', '')}")

        expertise = about.get("expertise", {})
        if expertise:
            parts.append("  Expertise:")
            for domain, skills in expertise.items():
                skill_str = _join_field(skills)
                parts.append(f"    - {domain}: {skill_str}")

        projects = about.get("projects", [])
        if projects:
            parts.append("  Projects:")
            for proj in projects:
                parts.append(f"    * {proj}")

    # --- Personality ---
    personality = data.get("personality", {})
    parts.append("\n[Personality]")
    parts.append(f"Style: {personality.get('style', '')}")
    parts.append(f"Languages: {_join_field(personality.get('languages', []))}")


    # consulting_style (Lina) atau coding_style (Kurisu) — keduanya didukung
    consulting_style = personality.get("consulting_style")
    coding_style = personality.get("coding_style")
    if consulting_style:
        parts.append(f"Consulting Style: {_join_field(consulting_style)}")
    if coding_style:
        parts.append(f"Coding Style: {_join_field(coding_style)}")

    # knowledge_focus bisa string atau list
    knowledge_focus = personality.get("knowledge_focus")
    if knowledge_focus:
        parts.append(f"Knowledge Focus: {_join_field(knowledge_focus)}")

    # --- Capabilities ---
    caps = data.get("capabilities", {})
    if caps:
        parts.append("\n[Capabilities]")
        for k, v in caps.items():
            parts.append(f"  {k}: {v}")

    # --- Memory Management (opsional, ada di Kurisu) ---
    memory = data.get("memory_management", {})
    if memory:
        parts.append("\n[Memory Management]")
        stm = memory.get("short_term_memory", {})
        ltm = memory.get("long_term_memory", {})
        if stm:
            parts.append(f"  Short-Term: {stm.get('update_rule', '')}")
        if ltm:
            parts.append(f"  Long-Term: {ltm.get('update_rule', '')}")

    # --- Rules ---
    rules = data.get("rules", [])
    if rules:
        parts.append("\n[Rules]")
        for r in rules:
            parts.append(f"  - {r}")

    # --- Noise Filtering ---
    nf = data.get("noise_filtering", {})
    if nf:
        parts.append("\n[Noise Filtering]")
        ignore = nf.get("ignore_patterns", [])
        parts.append(f"  Ignore Patterns: {_join_field(ignore)}")
        parts.append(f"  Behavior: {nf.get('behavior', '')}")

    # --- Functions ---
    functions = data.get("functions", [])
    if functions:
        parts.append("\n[Available Functions]")
        for fn in functions:
            parts.append(f"  - {fn}")

    # --- Examples ---
    examples = data.get("examples", [])
    if examples:
        parts.append("\n[Examples]")
        for ex in examples:
            parts.append(f"  - {ex}")

    # --- Contact Author ---
    ca = data.get("contact_author", {})
    if ca:
        parts.append("\n[Contact Author]")
        for k, v in ca.items():
            parts.append(f"  {k}: {v}")

    # --- Mood ---
    mood = data.get("mood", {})
    if mood:
        parts.append("\n[Mood System]")
        current = mood.get("current_mood", "neutral")
        parts.append(f"  Current Mood: {current}")
        thresholds = mood.get("mood_thresholds", {})
        if thresholds:
            parts.append(f"  Thresholds: {json.dumps(thresholds, ensure_ascii=False)}")

    # --- MCP Tools (Lina-style) ---
    mcp_tools = data.get("mcp_tools", {})
    if mcp_tools:
        parts.append("\n[MCP Tools Available]")
        for tool_name, tool_info in mcp_tools.items():
            desc = tool_info.get("description", "")
            when = tool_info.get("kapan_digunakan", [])
            parts.append(f"  Tool: {tool_name} — {desc}")
            if when:
                parts.append(f"  Kapan digunakan:")
                for w in when:
                    parts.append(f"    * {w}")
            guidance = tool_info.get("parameter_guidance", {})
            if guidance:
                parts.append(f"  Parameter guidance:")
                for k, v in guidance.items():
                    parts.append(f"    - {k}: {v}")

    # --- Product Output Format (Lina-style) ---
    pof = data.get("product_output_format", {})
    if pof:
        parts.append("\n[Product Output Format — WAJIB DIIKUTI]")
        parts.append(f"  Format: {pof.get('format', '')}")
        aturan = pof.get("aturan", [])
        if aturan:
            parts.append("  Aturan:")
            for a in aturan:
                parts.append(f"    - {a}")
        contoh = pof.get("contoh_output", "")
        if contoh:
            parts.append(f"  Contoh output yang benar:")
            parts.append(f"    {contoh}")

    # --- Default Instruction ---
    default = data.get("default", "")
    if default:
        parts.append("\n[Default Instruction]")
        parts.append(f"  {default}")

    return "\n".join(parts)


def build_prompt(
    session: Dict,
    history: str,
    retrieval: Optional[List[str]],
    new_input: str,
    user_info: Optional[Dict] = None,
    admin_role: Optional[str] = None,
    app_inventory: Optional[List[Dict]] = None,
) -> str:
    """
    Gabungkan session, history, retrieval, dan input baru jadi prompt LLM.

    session   : dict   -> data session (tanpa _id)
    history   : str    -> riwayat percakapan yang sudah diformat
    retrieval : list   -> hasil retrieval eksternal (opsional)
    new_input : str    -> input terbaru dari user
    user_info : dict   -> informasi profil pengguna terdaftar (nama, wa, email)
    admin_role: str    -> role admin aktif (superadmin/sales)
    app_inventory: list -> daftar aplikasi terinstal di HP Android (opsional)
    """

    # Format session context
    context = session.get("context", {})
    user_id = session.get('user_id', '-')
    is_guest = True
    if user_id != '-' and not str(user_id).startswith("guest"):
        is_guest = False
    
    auth_status = "VERIFIED (Logged In)" if not is_guest else "ANONYMOUS (Guest/Unverified)"

    session_info_lines = [
        f"Session ID           : {session.get('session_id', '-')}",
        f"User ID              : {user_id}",
        f"Auth Status          : {auth_status}",
        f"WhatsApp Mode        : {session.get('is_whatsapp', False)}",
        f"Current Intent       : {context.get('current_intent', 'browsing')}",
        f"Interested Products  : {', '.join(context.get('interested_products', [])) if context.get('interested_products') else '-'}",
        f"Last Booking ID      : {context.get('last_booking_id', '-')}",
        f"Summary              : {context.get('summary', '-')}",
        f"Keywords             : {context.get('keywords', '-')}",
        f"Last Message         : {context.get('last_message', '-')}"
    ]

    if admin_role:
        session_info_lines.append(f"Admin Dashboard Role: {admin_role}")

    if user_info:
        session_info_lines.append(f"User Registered Name  : {user_info.get('name', '-')}")
        session_info_lines.append(f"User Registered Phone : {user_info.get('phone', '-')}")
        session_info_lines.append(f"User Registered Email : {user_info.get('email', '-')}")

    session_info = "\n".join(session_info_lines)

    # Format app inventory
    app_inventory_block = ""
    if app_inventory:
        app_lines = []
        for app in app_inventory:
            label = app.get("label", "Unknown")
            pkg_name = app.get("packageName") or app.get("package_name") or "unknown"
            is_sys = app.get("isSystem") or app.get("is_system") or False
            sys_str = " (System App)" if is_sys else ""
            app_lines.append(f"  - {label}: {pkg_name}{sys_str}")
        app_inventory_block = "\n".join(app_lines)
    else:
        app_inventory_block = "  (No apps synced or inventory is empty)"

    # Format retrieval block
    retrieval_block = (
        "\n".join(retrieval) if retrieval else "(tidak ada data tambahan)"
    )

    # Gabungkan semua bagian
    prompt = f"""=== Session Info ===
{session_info}

=== Installed Apps on Android Device ===
{app_inventory_block}
(PERHATIAN/CRITICAL RULE: Jika pengguna meminta untuk membuka, menjalankan, atau menggunakan sebuah aplikasi, Anda WAJIB memeriksa daftar aplikasi terinstal di atas. Jika aplikasi yang diminta TIDAK ADA dalam daftar, JANGAN panggil tool `open_app`. Alih-alih, beritahu pengguna dengan sopan bahwa aplikasi tersebut tidak ditemukan atau tidak terinstal di perangkat mereka.)

=== Conversation History ===
{history}

=== Retrieved Knowledge ===
{retrieval_block}

=== New User Input ===
User: {new_input}""".strip()

    return prompt

