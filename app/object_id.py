from bson import ObjectId
from typing import Any
from pydantic_core import core_schema
from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler


class PyObjectId(ObjectId):
    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: Any, _handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_plain_validator_function(
            cls.validate,
            serialization=core_schema.plain_serializer_function_ser_schema(
                str, when_used="json"
            ),
        )

    @classmethod
    def validate(cls, input_value: Any) -> ObjectId:
        if isinstance(input_value, ObjectId):
            return input_value
        if isinstance(input_value, str) and ObjectId.is_valid(input_value):
            return ObjectId(input_value)
        raise ValueError("Invalid ObjectId")

    @classmethod
    def __get_pydantic_json_schema__(
        cls, _schema: core_schema.CoreSchema, _handler: GetJsonSchemaHandler
    ) -> dict:
        return {"type": "string", "examples": ["60f7c2b3c2a5f3e9a4d2a1c0"]}
