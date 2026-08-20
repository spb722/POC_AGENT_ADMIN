import json
import tempfile
import unittest
from pathlib import Path

from deepagents.backends import FilesystemBackend

from main import _strip_for_customer
from models import FieldEdit
from tools import build_tools, get_draft_fields


class CustomerVisibilityContractTests(unittest.TestCase):
    def _add_api_field(self, customer_visible=None):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        Path(temp_dir.name, "screen_2.json").write_text(
            json.dumps([{"id": "screen_2", "fields": []}]), encoding="utf-8"
        )
        backend = FilesystemBackend(root_dir=temp_dir.name)
        tools = {item.name: item for item in build_tools(backend, model=None)}
        config = {"configurable": {"thread_id": self.id()}}

        tools["load_screen_fields"].func("screen_2", config)
        edit = FieldEdit(
            op="add_field",
            path="serviceDetails.creditScore",
            field_label="Credit Score",
            origin="api",
            customer_visible=customer_visible,
        )
        tools["apply_field_edit"].func("screen_2", edit, config)
        return get_draft_fields(self.id(), "screen_2")[0]

    def test_api_field_is_customer_visible_by_default(self):
        field = self._add_api_field()
        self.assertEqual(field["origin"], "api")
        self.assertIs(field["customerVisible"], True)

    def test_explicit_hidden_request_is_preserved(self):
        field = self._add_api_field(customer_visible=False)
        self.assertIs(field["customerVisible"], False)

    def test_customer_response_does_not_infer_hidden_from_api_origin(self):
        fields = [
            {"path": "visible.apiValue", "origin": "api", "value": "600"},
            {
                "path": "hidden.internalValue",
                "origin": "api",
                "customerVisible": False,
                "value": "secret",
            },
        ]

        result = _strip_for_customer(fields)

        self.assertEqual([field["path"] for field in result], ["visible.apiValue"])


if __name__ == "__main__":
    unittest.main()
