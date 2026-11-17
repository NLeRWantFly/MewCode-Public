import json
from pydantic import BaseModel, Field
from mewcode.experts.recovery_context import verify_checkpoint
from mewcode.tools.read_recovery_context import ReadRecoveryContext
from mewcode.tools.base import ToolResult


class VerifyRecoveryCheckpointParams(BaseModel):
    node_id: str = ''
    evidence_tool_id: str = Field(min_length=1, description='Existing completed command tool ID from ReadRecoveryContext commands, or InspectVisualEvidence ID from observations. Verifies real host receipts without rerunning commands or requesting images.')
    case_id: str = Field(min_length=1)
    epoch: int = Field(ge=0)
    input_revision: str


class VerifyRecoveryCheckpoint(ReadRecoveryContext):
    name = 'VerifyRecoveryCheckpoint'
    description = 'Host-check an existing successful native command receipt or pixel observation against the original case/checkpoint. Does not rerun commands, send images, grant permissions, clear questions or complete business work. Only the host coordinator can act on callback_eligible.'
    params_model = VerifyRecoveryCheckpointParams

    async def execute(self, params):
        try:
            record = self.record(params.node_id)
            result = verify_checkpoint(record, self.node_id, params.evidence_tool_id,
                case_id=params.case_id, epoch=params.epoch, input_revision=params.input_revision)
            return ToolResult(json.dumps(result, ensure_ascii=False), host_observation={'recovery_checkpoint': result})
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)
