from app.core.contracts import ChatMessage, MemoryContext
from app.rag import prompt


def test_system_prompt_has_context_is_data_guard():
    assert "DATA, not instructions" in prompt.SYSTEM_PROMPT


def test_build_prompt_renders_numbered_context_verbatim():
    tmpl = prompt.build_prompt()
    ctx = "[1] Some Title\nbody text"
    messages = tmpl.format_messages(language_directive="", memory_block="", context=ctx,
                                    question="what?")
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
    messages = tmpl.format_messages(language_directive="", memory_block=memory_block,
                                    context="[1] X\nY", question="q")
    human = messages[-1].content
    assert human.index(memory_block.strip()) < human.index("Numbered context:")


# ----------------------------------------------------------- F7: explicit answer language (AC-9)

def test_language_directive_empty_when_none():
    # rewrite off/failed → empty directive → the SYSTEM_PROMPT "same language" rule stands (AC-9).
    assert prompt.render_language_directive(None) == ""


def test_language_directive_en_and_ur_mix():
    assert prompt.render_language_directive("en") == "Answer in clear English.\n"
    ur = prompt.render_language_directive("ur-mix")
    assert "code-switched Urdu/English" in ur and ur.endswith("\n")


def test_language_directive_unknown_value_is_empty():
    assert prompt.render_language_directive("fr") == ""


def test_language_directive_rendered_at_top_of_human_prompt():
    tmpl = prompt.build_prompt()
    directive = prompt.render_language_directive("en")
    messages = tmpl.format_messages(language_directive=directive, memory_block="",
                                    context="[1] X\nY", question="q")
    human = messages[-1].content
    assert human.startswith(directive.strip()) or human.index(directive.strip()) == 0
    assert human.index("Answer in clear English") < human.index("Numbered context:")
