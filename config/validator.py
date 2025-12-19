import logging
from jsonschema import validate, ValidationError
from .loader import load_schema

logger = logging.getLogger("meco.validator")


def validate_topology(parsed_yaml):
    """
    Validates the parsed YAML topology against the schema.
    Returns:
        dict: {"success": bool, "message": str}
    """
    try:
        schema = load_schema()
        if not schema:
            return {"success": False, "message": "Schema could not be loaded."}
        validate(instance=parsed_yaml, schema=schema)
        return {"success": True}
    except ValidationError as e:
        path = " -> ".join(str(p) for p in e.path) if e.path else "root"
        message = f"{path}: {e.message}"
        return {"success": False, "message": f"Validation failed: {message}"}
    except Exception as e:
        return {"success": False, "message": f"Validation error: {str(e)}"}
