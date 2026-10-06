"""A per-execution tool closure. The model cannot supply ownership context."""
from uuid import UUID

from langchain_core.tools import StructuredTool

from app.rag.casting_prompt import TOOL_DESCRIPTION, TOOL_NAME
from app.rag.casting_projection import project_result
from app.schemas.casting_graph import CastingToolInput
from app.services.conversation_repository import ConversationError, ConversationRepository


def casting_tool(service, identity):
    def generate_casting_design(input_file_id: str) -> dict:
        # Repeat the boundary check after native ToolNode validation and before IO.
        parsed = CastingToolInput(input_file_id=input_file_id)
        with service.session_factory() as db, db.begin():
            repo = ConversationRepository(db)
            turn = identity.validate(repo)
            if turn.effective_casting_input_file_id != UUID(parsed.input_file_id):
                raise ConversationError("CASTING_TOOL_INPUT_MISMATCH", "Tool input differs from the frozen turn.", status_code=409)
        result = service.execute_turn(identity.thread_id, identity.turn_id)
        return project_result(service, identity.thread_id, result.run_id)
    return StructuredTool.from_function(generate_casting_design, name=TOOL_NAME,
                                        description=TOOL_DESCRIPTION, args_schema=CastingToolInput)
