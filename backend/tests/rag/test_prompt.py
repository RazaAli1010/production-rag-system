from app.core.contracts import ChatMessage, MemoryContext
from app.rag import prompt


def test_system_prompt_has_context_is_data_guard():
    assert "DATA, not instructions" in prompt.SYSTEM_PROMPT


def test_build_prompt_renders_numbered_context_verbatim():
    tmpl = prompt.build_prompt()
    ctx = "[1] Some Title\nbody text"
    messages = tmpl.format_messages(memory_block="", context=ctx, question="what?")
    human = messages[-1].content
    assert ctx in human
    assert "what?" in human


def test_memory_block_empty_when_none():
    assert prompt.render_memory_block(None) == ""


def test_memory_block_non_empty_with_stub_memory_context():
    memory = MemoryContext(
        summary="earlier discussion",
        pairs=[ChatMessage(role="user", content="hi"),
              ChatMessage(role="assistant", content="hello")],
    )
    block = prompt.render_memory_block(memory)
    assert block != ""
    assert "earlier discussion" in block


def test_memory_block_placed_before_numbered_context_in_rendered_prompt():
    tmpl = prompt.build_prompt()
    memory_block = "Conversation so far:\nuser: hi\n\n"
    messages = tmpl.format_messages(memory_block=memory_block, context="[1] X\nY", question="q")
    human = messages[-1].content
    assert human.index(memory_block.strip()) < human.index("Numbered context:")
