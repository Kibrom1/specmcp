"""Unit tests for the Operation Model."""

from specmcp.core.model import (
    ArgumentBinding,
    ArgumentMap,
    AuthRequirement,
    Operation,
    Parameter,
    RequestBody,
    RequestBodyVariant,
    Response,
    ResponseVariant,
    SimplifiedOperation,
    SimplifyWarning,
)


def _petstore_operation() -> Operation:
    """Canonical Petstore getPetById operation from the design doc §5.3."""
    return Operation(
        id="getPetById",
        method="GET",
        path="/pets/{petId}",
        server_url="https://petstore.example.com/v1",
        parameters=[
            Parameter(
                name="petId",
                location="path",
                required=True,
                schema_={"type": "integer", "format": "int64"},
                style="simple",
                explode=False,
            ),
            Parameter(
                name="verbose",
                location="query",
                required=False,
                schema_={"type": "boolean", "default": False},
                style="form",
                explode=True,
            ),
        ],
        request_body=None,
        responses=[
            Response(
                status_code="200",
                description="A pet",
                variants=[
                    ResponseVariant(
                        content_type="application/json",
                        schema_={
                            "type": "object",
                            "required": ["id", "name"],
                            "properties": {
                                "id": {"type": "integer", "format": "int64"},
                                "name": {"type": "string"},
                                "tag": {"type": ["string", "null"]},
                            },
                        },
                    )
                ],
            ),
            Response(status_code="404", description="Not found"),
        ],
        auth=[[AuthRequirement(scheme_name="petstoreApiKey", scopes=[])]],
        summary="Get a pet by ID",
        source_location=("petstore.yaml", 8),
    )


# ---------------------------------------------------------------------------
# Operation instantiation
# ---------------------------------------------------------------------------


def test_operation_instantiation():
    op = _petstore_operation()
    assert op.id == "getPetById"
    assert op.method == "GET"
    assert op.path == "/pets/{petId}"
    assert op.server_url == "https://petstore.example.com/v1"
    assert len(op.parameters) == 2
    assert op.request_body is None
    assert len(op.responses) == 2
    assert len(op.auth) == 1
    assert op.auth[0][0].scheme_name == "petstoreApiKey"


def test_parameter_fields():
    op = _petstore_operation()
    pet_id = op.parameters[0]
    assert pet_id.name == "petId"
    assert pet_id.location == "path"
    assert pet_id.required is True
    assert pet_id.schema_ == {"type": "integer", "format": "int64"}
    assert pet_id.style == "simple"
    assert pet_id.explode is False


def test_response_variants():
    op = _petstore_operation()
    r200 = op.responses[0]
    assert r200.status_code == "200"
    assert len(r200.variants) == 1
    assert r200.variants[0].content_type == "application/json"
    assert r200.variants[0].is_binary is False


def test_operation_serialization_roundtrip():
    op = _petstore_operation()
    data = op.model_dump()
    op2 = Operation.model_validate(data)
    assert op2.id == op.id
    assert op2.parameters[0].schema_ == op.parameters[0].schema_


def test_nullable_normalized_form():
    """The 'tag' field should use the 3.1 form: type: ["string", "null"]."""
    op = _petstore_operation()
    schema = op.responses[0].variants[0].schema_
    assert "null" in schema["properties"]["tag"]["type"]


# ---------------------------------------------------------------------------
# Request body
# ---------------------------------------------------------------------------


def test_request_body_model():
    rb = RequestBody(
        required=True,
        variants=[
            RequestBodyVariant(
                content_type="application/json",
                schema_={"type": "object", "properties": {"name": {"type": "string"}}},
            )
        ],
    )
    assert rb.required is True
    assert rb.variants[0].content_type == "application/json"


# ---------------------------------------------------------------------------
# SimplifiedOperation
# ---------------------------------------------------------------------------


def test_simplified_operation_from_design_doc():
    """Matches §5.4 of the design doc exactly."""
    op = _petstore_operation()
    simplified = SimplifiedOperation(
        operation=op,
        llm_input_schema={
            "type": "object",
            "required": ["petId"],
            "properties": {
                "petId": {"type": "integer", "description": "The pet ID"},
                "verbose": {"type": "boolean", "default": False},
            },
            "additionalProperties": True,
        },
        llm_description="Get a pet by ID [GET /pets/{petId}]",
        arg_map=ArgumentMap(
            bindings={
                "petId": ArgumentBinding(
                    source_llm_key="petId",
                    target_kind="path",
                    target_path=["petId"],
                    style="simple",
                    explode=False,
                ),
                "verbose": ArgumentBinding(
                    source_llm_key="verbose",
                    target_kind="query",
                    target_path=["verbose"],
                    style="form",
                    explode=True,
                ),
            }
        ),
        warnings=[],
    )

    assert simplified.llm_description == "Get a pet by ID [GET /pets/{petId}]"
    assert "petId" in simplified.llm_input_schema["properties"]
    assert simplified.arg_map.bindings["petId"].target_kind == "path"
    assert simplified.arg_map.bindings["verbose"].target_kind == "query"
    assert simplified.warnings == []


def test_simplify_warning_model():
    w = SimplifyWarning(
        kind="fallback_to_freeform",
        operation_id="complexOp",
        field_path="/body",
        message="Schema too complex; using free-form JSON argument.",
    )
    assert w.kind == "fallback_to_freeform"
    assert w.operation_id == "complexOp"
