"""Small official-response shapes for deterministic source tests."""


def comtrade_codes(name, *, refresh=False):
    reporters = [
        {"code": "36", "label": "Australia"},
        {"code": "554", "label": "New Zealand"},
        *({"code": str(10000 + i), "label": f"Test reporter {i}"} for i in range(20)),
    ]
    partners = [
        {"code": "0", "label": "World"},
        *reporters,
        {"code": "9999", "label": "Additional partner area"},
    ]
    dimensions = {
        "REPORTER": reporters,
        "PARTNER": partners,
        "SECOND_PARTNER": partners,
        "HS": [
            {"code": "TOTAL", "label": "Total products"},
            {"code": "0901", "label": "Coffee"},
            {"code": "090111", "label": "Coffee, unroasted"},
        ],
        "FLOW": [{"code": "M", "label": "Imports"}, {"code": "X", "label": "Exports"}],
        "FREQUENCY": [{"code": "A", "label": "Annual"}, {"code": "M", "label": "Monthly"}],
        "TRANSPORT": [{"code": "0", "label": "Total"}, {"code": "1000", "label": "Air"}],
        "CUSTOMS": [{"code": "C00", "label": "Total"}, {"code": "C01", "label": "Test procedure"}],
    }
    if name not in dimensions:
        raise ValueError("Choose a Comtrade dimension ID from get_metadata.")
    return dimensions[name]
