from app.suite_check import (
    accounts_used,
    contradicts_code,
    exact_findings,
    exact_problems,
    selector_found,
)

CODE = """
export function LoginPage() {
  return (
    <form className="auth-form">
      <input name="email" placeholder="you@studio.com" />
      <button data-testid="submit" type="submit">Log in</button>
      <p className="error">Invalid email or password</p>
    </form>
  );
}
"""


def _found(selector: str) -> bool | None:
    return selector_found(selector, CODE, " ".join(CODE.split()).lower())


def test_selectors_are_looked_up_in_the_code() -> None:
    assert _found('[data-testid="submit"]') is True
    assert _found('[data-testid="sign-in"]') is False
    assert _found('button:has-text("Log in")') is True
    assert _found('button:has-text("Sign in")') is False
    assert _found('input[name="email"]') is True
    assert _found('input[placeholder="you@studio.com"]') is True
    assert _found("form.auth-form") is True
    assert _found('input[type="password"]') is None


def test_a_case_is_stale_only_for_what_the_code_lost() -> None:
    case = {
        "title": "Valid sign-in",
        "steps": [
            {
                "label": "Submit",
                "actions": [
                    {"type": "click", "selectors": ['button:has-text("Sign in")']},
                    {"type": "waitForText", "text": "Invalid email or password"},
                ],
            },
            {"label": "Open", "actions": [{"type": "navigate", "path": "/gone"}]},
        ],
    }
    problems = exact_problems(case, CODE, ["/", "/login"])
    assert len(problems) == 2
    assert 'button:has-text("Sign in")' in problems[0]
    assert "/gone" in problems[1]


def test_text_built_from_variables_counts_as_present() -> None:
    code = "<StudioFrame title={`Hello, ${user?.name ?? 'there'}`} />\n<h2>Hi, {name}</h2>"
    case = {
        "steps": [
            {
                "label": "Studio",
                "actions": [
                    {"type": "waitForText", "text": "Hello, Tursor"},
                    {"type": "click", "selectors": ['h2:has-text("Hi, Navaneeth")']},
                ],
            }
        ]
    }
    assert exact_problems(case, code, []) == []
    missing = {"steps": [{"label": "x", "actions": [{"type": "waitForText", "text": "Goodbye, Tursor"}]}]}
    assert len(exact_problems(missing, code, [])) == 1


def test_saved_accounts_are_listed_without_passwords() -> None:
    suite = {
        "cases": [
            {
                "kind": "success",
                "steps": [
                    {
                        "label": "Fill credentials",
                        "actions": [
                            {"type": "fill", "selectors": ['input[name="email"]'], "value": "tursor@gmail.com"},
                            {"type": "fill", "selectors": ['input[type="password"]'], "value": "secret1"},
                        ],
                    }
                ],
            },
            {
                "kind": "failure",
                "steps": [
                    {"label": "Bad", "actions": [{"type": "fill", "selectors": ['input[name="email"]'], "value": "nope"}]}
                ],
            },
        ]
    }
    used = accounts_used(suite)
    assert used == {"accounts": ["tursor@gmail.com"], "password_saved": True}
    assert "secret1" not in str(used)


def test_a_model_reason_about_text_the_code_has_is_dropped() -> None:
    confirmed = ["Hello, Tursor", '[data-testid="submit"]']
    assert contradicts_code(
        'After signing in, the Studio page does not render the text "Hello, Tursor".',
        confirmed,
    )
    assert contradicts_code("The greeting Hello, Tursor is no longer shown", confirmed)
    # A reason about something else, or with nothing confirmed, stands.
    assert not contradicts_code('The button now says "Log in", not "Sign in".', confirmed)
    assert not contradicts_code('The text "Hello, Tursor" is gone.', [])


def test_texts_found_in_the_code_are_reported() -> None:
    code = "const title = `Hello, ${user?.name ?? 'there'}`;"
    case = {
        "steps": [
            {"label": "Studio", "actions": [{"type": "waitForText", "text": "Hello, Tursor"}]}
        ]
    }
    problems, found = exact_findings(case, code, [])
    assert problems == []
    assert found == ["Hello, Tursor"]
