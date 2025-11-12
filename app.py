def run_agent_turn(call_sid: str, user_text: str) -> str:
    """
    Build an agent with tools bound to this call, and let it decide the next response.
    """
    from langchain.agents import initialize_agent, AgentType
    tools = make_tools_for_call(call_sid)

    s = load_session(call_sid)
    history_lines = []
    for h in s.get("history", [])[-6:]:
        if h.get("user"):
            history_lines.append(f"User: {h['user']}")
        if h.get("ai"):
            history_lines.append(f"Assistant: {h['ai']}")
    context = "\n".join(history_lines)

    prompt = (
        f"{AGENT_SYSTEM_PROMPT}\n"
        f"Caller number: {s.get('caller')}\n"
        f"Conversation so far:\n{context}\n\n"
        f"User just said: {user_text}\n"
        f"Think step-by-step, call tools as needed. Then answer with what to SAY."
    )

    agent = initialize_agent(
        tools,
        llm,
        agent_type=AgentType.ZERO_SHOT_REACT_DESCRIPTION,
        verbose=True,
    )

    print("🤖 Agent prompt start ----")
    print(prompt)
    print("---- Agent prompt end 🤖")

    # ✅ FIX: use invoke() instead of run()
    result = agent.invoke({"input": prompt})
    spoken = result.get("output", result) if isinstance(result, dict) else result

    s["history"].append({"user": user_text, "ai": spoken})
    save_session(call_sid, s)

    return spoken
