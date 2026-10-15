from __future__ import annotations

import types


def _make_step(failed_step: str | None, close_fails: bool, error: RuntimeError, calls: list[str]):
    close_error = RuntimeError("secondary close failure")

    def step(name: str):
        calls.append(name)
        if name == failed_step:
            raise error
        if name == "close" and close_fails:
            raise close_error

    return step


def make_browser(failed_step: str | None, close_fails: bool, url: str, error: RuntimeError):
    calls: list[str] = []
    step = _make_step(failed_step, close_fails, error, calls)

    def click(**kwargs) -> None:
        assert kwargs == {"no_wait_after": True}
        step("submit")

    def locator(selector: str):
        name = {"#username": "username", "#password": "password", "#kc-login": "submit"}[selector]
        return types.SimpleNamespace(
            wait_for=lambda **_kwargs: step("visibility"),
            fill=lambda _value: step(name),
            click=click,
        )

    def goto(attempted_url: str, **kwargs) -> None:
        assert attempted_url == url
        assert kwargs == {"wait_until": "domcontentloaded"}
        step("goto")

    def wait_for_url(pattern: str, **kwargs) -> None:
        assert pattern == "**/oauth2/**"
        assert kwargs == {"timeout": 10_000}
        step("wait-callback")

    page = types.SimpleNamespace(
        goto=goto,
        locator=locator,
        wait_for_url=wait_for_url,
    )

    def new_page():
        step("new-page")
        return page

    browser = types.SimpleNamespace(new_page=new_page, close=lambda: step("close"))

    def launch():
        step("launch")
        return browser

    def before_callback() -> None:
        step("before-callback")

    return launch, before_callback, calls
