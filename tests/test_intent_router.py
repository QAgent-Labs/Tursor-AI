from app.intent_router import features_named, is_out_of_scope, names_a_flow, wants_generation


def test_a_process_question_does_not_name_print_checkout() -> None:
    catalog = [{"feature": "Print checkout", "details": "Buy print", "embedding": [0.1]}]
    assert features_named("hi i need to know how to test a feature", "", catalog) == []


def test_naming_checkout_selects_that_feature() -> None:
    catalog = [
        {"feature": "Print checkout", "details": "Buy print"},
        {"feature": "Sign in", "details": "Email and password"},
    ]
    named = features_named("How does print checkout work?", "", catalog)
    assert [item["feature"] for item in named] == ["Print checkout"]
    assert "embedding" not in named[0]


def test_weather_is_out_of_scope() -> None:
    assert is_out_of_scope("What's the weather?")


def test_generate_is_a_suite_request() -> None:
    assert wants_generation("okay create me the cdp steps for this flow please")
    assert not wants_generation("hi i need to know how to test a feature")


def test_a_named_flow_is_not_a_testing_concept() -> None:
    assert names_a_flow("i want to test the account creation flow", "")
    assert names_a_flow("how does account creation work?", "")
    assert not names_a_flow("hi i need to know how to test a feature", "")
    assert not names_a_flow("What is regression testing?", "")
