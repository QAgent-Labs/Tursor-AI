from app.selector_grounding import ground_suite


def test_wrong_tag_for_a_known_class_is_rewritten() -> None:
    source = """
    <label className={`flow-field ${tone}`}>
      <span className="flow-field__label">{label}</span>
      <input />
    </label>
    """
    suite = {
        "feature": "Sign up",
        "cases": [
            {
                "kind": "success",
                "title": "Fill email",
                "explanation": "",
                "steps": [
                    {
                        "id": "fill-email",
                        "label": "Fill Email",
                        "actions": [
                            {
                                "type": "fill",
                                "selectors": ['div.flow-field:has-text("Email") input'],
                                "value": "navaneeth@gmail.com",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    grounded = ground_suite(suite, [source])
    assert grounded is not None
    selectors = grounded["cases"][0]["steps"][0]["actions"][0]["selectors"]
    assert selectors == ['label.flow-field:has-text("Email") input']


def test_selector_without_a_class_is_left_alone() -> None:
    suite = {
        "feature": "Login",
        "cases": [
            {
                "kind": "success",
                "title": "Open",
                "explanation": "",
                "steps": [
                    {
                        "id": "click",
                        "label": "Click",
                        "actions": [
                            {
                                "type": "click",
                                "selectors": ['[data-testid="get-started"]', 'button:has-text("Get started")'],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    grounded = ground_suite(suite, ["export const ok = true;\n"])
    assert grounded == suite
