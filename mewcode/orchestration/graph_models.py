"""Credential-free model routes for source-grounded graph generation."""


def available_model_routes(registry):
    rows = []
    for record in registry.records():
        if record.error:
            continue
        models = list(dict.fromkeys([*([record.provider.model] if record.provider.model else []), *record.models]))
        rows.append({"provider_key":record.key, "label":record.provider.name,
                     "models":[{"model_id":model,
                                "efforts":registry._effort_levels(record.provider.protocol,model,record)}
                               for model in models]})
    return rows
