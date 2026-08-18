"""Pydantic schemas shared by tools.py and agent.py.

These mirror the fixed screen_N.json field-object contract described in the
build spec: {type, path, fieldLabel, controlType, dataType, required, value,
origin, cascade?, values?}. `values` (options for dropdown/radio) may be
absent entirely or an empty list -- both occur in the real seed data.
"""

from typing import Literal

from pydantic import BaseModel, Field


class ScreenClassification(BaseModel):
    """Output of classify_screen: which screen file(s) an admin message targets."""

    screens: list[str] = Field(description="Screen ids, e.g. ['screen_1', 'screen_2']")
    confidence: Literal["high", "low"]
    reasoning: str = Field(description="Short rationale, for logging/debugging only")


class FieldCandidate(BaseModel):
    """A field shown to the admin to disambiguate when classify_field can't be sure."""

    path: str
    field_label: str
    control_type: str


class FieldMatch(BaseModel):
    """Output of classify_field. 'not found' is a first-class outcome, not an empty list."""

    found: bool
    matched_path: str | None = Field(
        default=None, description="Set only when found is True"
    )
    candidates: list[FieldCandidate] = Field(
        default_factory=list, description="Set only when found is False"
    )


EditOp = Literal[
    "add_field",
    "delete_field",
    "rename_field",
    "add_option",
    "rename_option",
    "remove_option",
    "set_default_value",
]


class FieldEdit(BaseModel):
    """The structured edit instruction the agent passes to check_noop / apply_field_edit.

    Only the fields relevant to `op` need to be set; unused fields are ignored.
    `path` never changes an existing field's identity -- for add_field it is the
    path of the brand-new field being created.
    """

    op: EditOp
    path: str = Field(description="Target field's stable path (dot notation, may include [*])")

    # add_field / rename_field
    field_label: str | None = Field(default=None, description="New display label")
    control_type: str | None = Field(default=None, description="add_field only, e.g. 'dropdown', 'text'")
    data_type: str | None = Field(default=None, description="add_field only, e.g. 'list', 'text'")
    required: bool | None = Field(default=None, description="add_field only, defaults to False")
    default_value: str | None = Field(
        default=None,
        description="add_field: initial 'value' for the new field. set_default_value: the new 'value' for an existing field.",
    )

    # add_option / rename_option / remove_option (values[] entries)
    option_value: str | None = Field(default=None, description="The values[].value being targeted")
    option_label: str | None = Field(default=None, description="New/added values[].label")
    new_option_value: str | None = Field(default=None, description="rename_option only, if the value code itself changes")


class ChatRequest(BaseModel):
    session_id: str
    message: str


ChatStatus = Literal["ok", "pending_confirmation", "rejected", "info"]


class ChatResponse(BaseModel):
    reply: str
    pending_diff: dict | None = None
    awaiting_confirmation: bool
    status: ChatStatus = Field(
        description="'ok' = written to disk, refetch screen_ids. 'pending_confirmation' = "
        "diff staged for review only, do not refetch yet. 'rejected' = discarded, nothing "
        "written. 'info' = no diff exists this turn, just read `reply`."
    )
    screen_ids: list[str] = Field(
        default_factory=list,
        description="Screens this turn touched. Only meaningful as a refetch signal when status=='ok'.",
    )
    run_id: str = Field(description="Grep this in logs/agent.log to see everything this turn did")
