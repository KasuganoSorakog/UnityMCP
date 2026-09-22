import asyncio
import json
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


fastmcp_middleware = ModuleType("fastmcp.server.middleware")


class Middleware:
    pass


class MiddlewareContext:
    pass


fastmcp_middleware.Middleware = Middleware
fastmcp_middleware.MiddlewareContext = MiddlewareContext
sys.modules.setdefault("fastmcp", ModuleType("fastmcp"))
sys.modules.setdefault("fastmcp.server", ModuleType("fastmcp.server"))
sys.modules["fastmcp.server.middleware"] = fastmcp_middleware

# The middleware serializes classified guard errors via lazily-imported
# fastmcp/mcp result types; stub them (only when the real modules are not
# already loaded) so this file also works standalone.
fastmcp_tools_tool = sys.modules.setdefault(
    "fastmcp.tools.tool", ModuleType("fastmcp.tools.tool"))


if not hasattr(fastmcp_tools_tool, "ToolResult"):
    class ToolResult:
        def __init__(self, content=None, structured_content=None, meta=None):
            self.content = content
            self.structured_content = structured_content
            self.meta = meta

    fastmcp_tools_tool.ToolResult = ToolResult

mcp_types = sys.modules.setdefault("mcp.types", ModuleType("mcp.types"))


if not hasattr(mcp_types, "TextContent"):
    class TextContent:
        def __init__(self, type="text", text=""):
            self.type = type
            self.text = text

    mcp_types.TextContent = TextContent

mcp_helper_types = sys.modules.setdefault(
    "mcp.server.lowlevel.helper_types",
    ModuleType("mcp.server.lowlevel.helper_types"))


if not hasattr(mcp_helper_types, "ReadResourceContents"):
    class ReadResourceContents:
        def __init__(self, content, mime_type=None, meta=None):
            self.content = content
            self.mime_type = mime_type
            self.meta = meta

    mcp_helper_types.ReadResourceContents = ReadResourceContents

plugin_hub_module = ModuleType("transport.plugin_hub")


class PluginHub:
    configured = False

    @classmethod
    def is_configured(cls):
        return cls.configured

    @classmethod
    async def _resolve_session_id(cls, unity_instance):
        raise RuntimeError("not connected")


class PluginDisconnectedError(RuntimeError):
    """Mirror of transport.plugin_hub.PluginDisconnectedError (stubbed here to
    avoid importing the real module's heavy dependencies). The real class is
    contract-tested in test_plugin_disconnected_error.py."""

    def __init__(self, message, *, code=None, category=None, retryable=None, retry_after_ms=None, hint=None):
        super().__init__(message)
        self.code = code
        self.category = category
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms
        self.hint = hint


def classified_error_response(*, code, category, error, severity="warning",
                              retryable=False, retry_after_ms=None, hint=None,
                              data=None):
    """Mirror of transport.plugin_hub.classified_error_response (stubbed here
    for the same reason as PluginDisconnectedError)."""
    payload = dict(data or {})
    payload.setdefault("reason", code)
    if retry_after_ms is not None:
        payload.setdefault("retry_after_ms", retry_after_ms)
    return {
        "success": False,
        "message": None,
        "error": error,
        "data": payload or None,
        "hint": hint,
        "code": code,
        "category": category,
        "severity": severity,
        "retryable": retryable,
        "retry_after_ms": retry_after_ms,
    }


plugin_hub_module.PluginHub = PluginHub
plugin_hub_module.PluginDisconnectedError = PluginDisconnectedError
plugin_hub_module.classified_error_response = classified_error_response
sys.modules["transport.plugin_hub"] = plugin_hub_module

import transport.unity_instance_middleware as unity_instance_middleware

from transport.unity_instance_middleware import UnityInstanceMiddleware


def make_codex_context(thread_id, workspace_root="E:/Project", server_name="UnityMCP"):
    return SimpleNamespace(
        client_id=None,
        session_id=None,
        request_context=SimpleNamespace(
            client_id=None,
            session_id="shared-fastmcp-session",
            meta={
                "x-codex-turn-metadata": {
                    "thread_id": thread_id,
                    "workspaces": [{"root": workspace_root}],
                    "mcp_server": server_name,
                }
            },
        ),
    )


class UnityInstanceMiddlewareTests(unittest.TestCase):
    def test_active_instances_are_isolated_between_codex_threads(self):
        middleware = UnityInstanceMiddleware()
        ctx_a = make_codex_context("thread-a")
        ctx_b = make_codex_context("thread-b")

        middleware.set_active_instance(ctx_a, "ProjectA@aaa111")
        middleware.set_active_instance(ctx_b, "ProjectB@bbb222")

        self.assertEqual(middleware.get_active_instance(ctx_a), "ProjectA@aaa111")
        self.assertEqual(middleware.get_active_instance(ctx_b), "ProjectB@bbb222")

    def test_clear_active_instance_only_clears_current_session_key(self):
        middleware = UnityInstanceMiddleware()
        ctx_a = make_codex_context("thread-a")
        ctx_b = make_codex_context("thread-b")

        middleware.set_active_instance(ctx_a, "ProjectA@aaa111")
        middleware.set_active_instance(ctx_b, "ProjectB@bbb222")
        middleware.clear_active_instance(ctx_a)

        self.assertIsNone(middleware.get_active_instance(ctx_a))
        self.assertEqual(middleware.get_active_instance(ctx_b), "ProjectB@bbb222")

    def test_active_instance_survives_tool_resource_identity_shape_changes(self):
        middleware = UnityInstanceMiddleware()
        tool_ctx = make_codex_context(
            "thread-a",
            workspace_root="E:/Project",
            server_name="UnityMCP_8080",
        )
        resource_ctx = SimpleNamespace(
            client_id=None,
            session_id=None,
            request_context=SimpleNamespace(
                client_id=None,
                session_id="different-fastmcp-session",
                meta={
                    "x-codex-turn-metadata": {
                        "thread_id": "thread-a",
                    }
                },
            ),
        )

        middleware.set_active_instance(tool_ctx, "ProjectA@aaa111")

        self.assertEqual(
            middleware.get_active_instance(resource_ctx),
            "ProjectA@aaa111",
        )

    def test_set_active_instance_rejects_unkeyed_context(self):
        middleware = UnityInstanceMiddleware()
        ctx = SimpleNamespace(client_id=None, session_id=None, request_context=None)

        with self.assertRaisesRegex(ValueError, "stable MCP client session key"):
            middleware.set_active_instance(ctx, "ProjectA@aaa111")

    def test_active_instance_map_is_lru_bounded(self):
        middleware = UnityInstanceMiddleware()
        middleware._MAX_ACTIVE_KEYS = 4

        def ctx_for(i):
            return SimpleNamespace(
                client_id=f"client-{i}", session_id=None, request_context=None)

        for i in range(6):
            middleware.set_active_instance(ctx_for(i), f"Project@hash-{i}")

        self.assertEqual(len(middleware._active_by_key), 4)
        # Oldest entries were evicted.
        self.assertIsNone(middleware.get_active_instance(ctx_for(0)))
        self.assertIsNone(middleware.get_active_instance(ctx_for(1)))
        # Newest entries survive.
        self.assertEqual(
            middleware.get_active_instance(ctx_for(5)), "Project@hash-5")
        self.assertEqual(
            middleware.get_active_instance(ctx_for(4)), "Project@hash-4")

    def test_get_active_instance_refreshes_lru_position(self):
        middleware = UnityInstanceMiddleware()
        middleware._MAX_ACTIVE_KEYS = 3

        contexts = [
            SimpleNamespace(
                client_id=f"client-{i}", session_id=None, request_context=None)
            for i in range(4)
        ]
        for i in range(3):
            middleware.set_active_instance(contexts[i], f"Project@hash-{i}")

        # Touch client-0 so client-1 becomes the least recently used.
        self.assertEqual(
            middleware.get_active_instance(contexts[0]), "Project@hash-0")

        middleware.set_active_instance(contexts[3], "Project@hash-3")

        self.assertEqual(len(middleware._active_by_key), 3)
        self.assertEqual(
            middleware.get_active_instance(contexts[0]), "Project@hash-0")
        self.assertIsNone(middleware.get_active_instance(contexts[1]))

    def test_unreachable_active_instance_raises_retryable_and_keeps_selection(self):
        class ContextState(SimpleNamespace):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.state = {}

            def set_state(self, key, value):
                self.state[key] = value

        class MiddlewareContext:
            def __init__(self, fastmcp_context):
                self.fastmcp_context = fastmcp_context

        async def run_test():
            middleware = UnityInstanceMiddleware()
            # The unreachable guard only applies to HTTP transport.
            middleware._is_http_transport = lambda: True
            ctx = ContextState(
                client_id="client-a",
                session_id=None,
                request_context=None,
            )
            middleware.set_active_instance(ctx, "ProjectA@aaa111")
            old_plugin_hub = unity_instance_middleware.PluginHub
            unity_instance_middleware.PluginHub = PluginHub
            PluginHub.configured = True
            try:
                with self.assertRaises(unity_instance_middleware.PluginDisconnectedError):
                    await middleware._inject_unity_instance(MiddlewareContext(ctx))
            finally:
                PluginHub.configured = False
                unity_instance_middleware.PluginHub = old_plugin_hub

            # Multi-project safety: a transient unreachable window (domain reload,
            # sweeper grace) must NOT clear the selection and re-route to whatever
            # project is the only one online. The request fails as retryable and
            # the stored selection survives.
            self.assertEqual(
                middleware.get_active_instance(ctx), "ProjectA@aaa111")
            self.assertNotIn("unity_instance", ctx.state)

        asyncio.run(run_test())

    def test_selection_management_tool_exempt_from_unreachable_guard(self):
        class ContextState(SimpleNamespace):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.state = {}

            def set_state(self, key, value):
                self.state[key] = value

        class MiddlewareContext:
            def __init__(self, fastmcp_context, message=None):
                self.fastmcp_context = fastmcp_context
                self.message = message

        async def run_test():
            middleware = UnityInstanceMiddleware()
            middleware._is_http_transport = lambda: True
            ctx = ContextState(
                client_id="client-a",
                session_id=None,
                request_context=None,
            )
            middleware.set_active_instance(ctx, "ProjectA@aaa111")
            old_plugin_hub = unity_instance_middleware.PluginHub
            unity_instance_middleware.PluginHub = PluginHub
            PluginHub.configured = True
            try:
                # The escape hatch: set_active_instance must stay callable even
                # though the stored selection is unreachable.
                await middleware._inject_unity_instance(
                    MiddlewareContext(ctx, SimpleNamespace(name="set_active_instance")))

                # Every other tool still fails fast as retryable.
                with self.assertRaises(unity_instance_middleware.PluginDisconnectedError):
                    await middleware._inject_unity_instance(
                        MiddlewareContext(ctx, SimpleNamespace(name="manage_scene")))
            finally:
                PluginHub.configured = False
                unity_instance_middleware.PluginHub = old_plugin_hub

            # Selection survives both paths.
            self.assertEqual(
                middleware.get_active_instance(ctx), "ProjectA@aaa111")

        asyncio.run(run_test())

    def test_instances_resource_exempt_from_unreachable_guard(self):
        class ContextState(SimpleNamespace):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.state = {}

            def set_state(self, key, value):
                self.state[key] = value

        class MiddlewareContext:
            def __init__(self, fastmcp_context, message=None):
                self.fastmcp_context = fastmcp_context
                self.message = message

        async def run_test():
            middleware = UnityInstanceMiddleware()
            middleware._is_http_transport = lambda: True
            ctx = ContextState(
                client_id="client-a",
                session_id=None,
                request_context=None,
            )
            middleware.set_active_instance(ctx, "ProjectA@aaa111")
            old_plugin_hub = unity_instance_middleware.PluginHub
            unity_instance_middleware.PluginHub = PluginHub
            PluginHub.configured = True
            try:
                # Resource messages carry `.uri` instead of `.name`. The instance
                # listing is the enumeration entry point that set_active_instance's
                # own error text points to, so it must stay readable even when the
                # stored selection is dead.
                await middleware._inject_unity_instance(
                    MiddlewareContext(ctx, SimpleNamespace(uri="mcpforunity://instances")))
                # AnyUrl normalization may add a trailing slash; that form must
                # also be recognized.
                await middleware._inject_unity_instance(
                    MiddlewareContext(ctx, SimpleNamespace(uri="mcpforunity://instances/")))

                # Other resources still fail fast as retryable.
                with self.assertRaises(unity_instance_middleware.PluginDisconnectedError):
                    await middleware._inject_unity_instance(
                        MiddlewareContext(ctx, SimpleNamespace(uri="mcpforunity://custom-tools")))
            finally:
                PluginHub.configured = False
                unity_instance_middleware.PluginHub = old_plugin_hub

            # Selection survives both paths.
            self.assertEqual(
                middleware.get_active_instance(ctx), "ProjectA@aaa111")

        asyncio.run(run_test())

    def test_unreachable_error_carries_classification_fields(self):
        class ContextState(SimpleNamespace):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.state = {}

            def set_state(self, key, value):
                self.state[key] = value

        class MiddlewareContext:
            def __init__(self, fastmcp_context, message=None):
                self.fastmcp_context = fastmcp_context
                self.message = message

        async def run_test():
            middleware = UnityInstanceMiddleware()
            middleware._is_http_transport = lambda: True
            ctx = ContextState(
                client_id="client-a",
                session_id=None,
                request_context=None,
            )
            middleware.set_active_instance(ctx, "ProjectA@aaa111")
            old_plugin_hub = unity_instance_middleware.PluginHub
            unity_instance_middleware.PluginHub = PluginHub
            PluginHub.configured = True
            try:
                with self.assertRaises(unity_instance_middleware.PluginDisconnectedError) as caught:
                    await middleware._inject_unity_instance(
                        MiddlewareContext(ctx, SimpleNamespace(name="manage_scene")))
            finally:
                PluginHub.configured = False
                unity_instance_middleware.PluginHub = old_plugin_hub

            exc = caught.exception
            # Raise-style paths must expose the same machine-readable contract as
            # classified_error_response on the send_command path.
            self.assertEqual(exc.code, "unity_instance_unreachable")
            self.assertEqual(exc.category, "session")
            self.assertIs(exc.retryable, True)
            self.assertEqual(exc.retry_after_ms, 2000)
            self.assertEqual(exc.hint, "retry")

        asyncio.run(run_test())

    def test_stdio_transport_preserves_active_instance_on_pluginhub_miss(self):
        class ContextState(SimpleNamespace):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.state = {}

            def set_state(self, key, value):
                self.state[key] = value

        class MiddlewareContext:
            def __init__(self, fastmcp_context):
                self.fastmcp_context = fastmcp_context

        async def run_test():
            middleware = UnityInstanceMiddleware()
            # stdio mode: PluginHub is configured but its registry is empty;
            # an explicit selection must not be cleared.
            middleware._is_http_transport = lambda: False
            ctx = ContextState(
                client_id="client-a",
                session_id=None,
                request_context=None,
            )
            middleware.set_active_instance(ctx, "ProjectA@aaa111")
            old_plugin_hub = unity_instance_middleware.PluginHub
            unity_instance_middleware.PluginHub = PluginHub
            PluginHub.configured = True
            try:
                await middleware._inject_unity_instance(MiddlewareContext(ctx))
            finally:
                PluginHub.configured = False
                unity_instance_middleware.PluginHub = old_plugin_hub

            self.assertEqual(
                middleware.get_active_instance(ctx), "ProjectA@aaa111")
            self.assertEqual(ctx.state.get("unity_instance"), "ProjectA@aaa111")

        asyncio.run(run_test())

    def test_is_http_transport_reflects_current_transport(self):
        fake_unity_transport = ModuleType("transport.unity_transport")
        fake_unity_transport._current_transport = lambda: "http"
        with patch.dict(sys.modules, {"transport.unity_transport": fake_unity_transport}):
            self.assertTrue(UnityInstanceMiddleware._is_http_transport())
            fake_unity_transport._current_transport = lambda: "stdio"
            self.assertFalse(UnityInstanceMiddleware._is_http_transport())

    def _make_state_context(self):
        class ContextState(SimpleNamespace):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.state = {}

            def set_state(self, key, value):
                self.state[key] = value

        return ContextState(
            client_id="client-a",
            session_id=None,
            request_context=None,
        )

    def test_on_call_tool_returns_classified_json_for_dead_selection(self):
        async def run_test():
            middleware = UnityInstanceMiddleware()
            middleware._is_http_transport = lambda: True
            ctx = self._make_state_context()
            middleware.set_active_instance(ctx, "ProjectA@aaa111")
            old_plugin_hub = unity_instance_middleware.PluginHub
            unity_instance_middleware.PluginHub = PluginHub
            PluginHub.configured = True
            call_next_called = False

            async def call_next(context):
                nonlocal call_next_called
                call_next_called = True
                return "should-not-happen"

            try:
                context = SimpleNamespace(
                    fastmcp_context=ctx,
                    message=SimpleNamespace(name="manage_scene"),
                )
                result = await middleware.on_call_tool(context, call_next)
            finally:
                PluginHub.configured = False
                unity_instance_middleware.PluginHub = old_plugin_hub

            # The guard must short-circuit: call_next is never invoked, and the
            # classified fields reach the client as JSON text content — the same
            # shape send_command-path errors use.
            self.assertFalse(call_next_called)
            payload = json.loads(result.content[0].text)
            self.assertFalse(payload["success"])
            self.assertEqual(payload["code"], "unity_instance_unreachable")
            self.assertEqual(payload["category"], "session")
            self.assertIs(payload["retryable"], True)
            self.assertEqual(payload["retry_after_ms"], 2000)
            self.assertEqual(payload["hint"], "retry")
            structured = getattr(result, "structured_content", None)
            if structured is not None:
                self.assertEqual(
                    structured["code"], "unity_instance_unreachable")

        asyncio.run(run_test())

    def test_on_read_resource_returns_classified_json_for_dead_selection(self):
        async def run_test():
            middleware = UnityInstanceMiddleware()
            middleware._is_http_transport = lambda: True
            ctx = self._make_state_context()
            middleware.set_active_instance(ctx, "ProjectA@aaa111")
            old_plugin_hub = unity_instance_middleware.PluginHub
            unity_instance_middleware.PluginHub = PluginHub
            PluginHub.configured = True
            call_next_called = False

            async def call_next(context):
                nonlocal call_next_called
                call_next_called = True
                return "should-not-happen"

            try:
                context = SimpleNamespace(
                    fastmcp_context=ctx,
                    message=SimpleNamespace(uri="mcpforunity://custom-tools"),
                )
                result = await middleware.on_read_resource(context, call_next)
            finally:
                PluginHub.configured = False
                unity_instance_middleware.PluginHub = old_plugin_hub

            self.assertFalse(call_next_called)
            contents = list(result)
            self.assertEqual(len(contents), 1)
            payload = json.loads(contents[0].content)
            self.assertFalse(payload["success"])
            self.assertEqual(payload["code"], "unity_instance_unreachable")
            self.assertEqual(payload["category"], "session")
            self.assertIs(payload["retryable"], True)
            self.assertEqual(payload["retry_after_ms"], 2000)
            self.assertEqual(payload["hint"], "retry")

        asyncio.run(run_test())

    def test_on_call_tool_passes_through_when_no_dead_selection(self):
        async def run_test():
            middleware = UnityInstanceMiddleware()
            middleware._is_http_transport = lambda: True
            ctx = SimpleNamespace(
                client_id="client-a", session_id=None, request_context=None)
            call_next_called = False

            async def call_next(context):
                nonlocal call_next_called
                call_next_called = True
                return "tool-result"

            context = SimpleNamespace(
                fastmcp_context=ctx,
                message=SimpleNamespace(name="manage_scene"),
            )
            result = await middleware.on_call_tool(context, call_next)
            self.assertTrue(call_next_called)
            self.assertEqual(result, "tool-result")

        asyncio.run(run_test())

    def test_classified_guard_reraises_non_classified_exceptions(self):
        async def run_test():
            middleware = UnityInstanceMiddleware()

            async def broken_inject(context):
                raise ValueError("unexpected failure")

            middleware._inject_unity_instance = broken_inject

            async def call_next(context):
                return "should-not-happen"

            context = SimpleNamespace(
                fastmcp_context=SimpleNamespace(
                    client_id="client-a", session_id=None, request_context=None),
                message=SimpleNamespace(name="manage_scene"),
            )
            # Non-classified errors must still propagate unchanged.
            with self.assertRaises(ValueError):
                await middleware.on_call_tool(context, call_next)
            with self.assertRaises(ValueError):
                await middleware.on_read_resource(context, call_next)

        asyncio.run(run_test())

    def test_classified_payload_defaults_retryable_true_for_bare_exception(self):
        # A bare PluginDisconnectedError (no classification fields set) must not
        # produce retryable=False — the guard payload means "temporarily
        # unreachable, safe to retry", so the fallback must be True.
        from transport.unity_instance_middleware import _classified_error_payload

        bare = unity_instance_middleware.PluginDisconnectedError("gone")
        payload = _classified_error_payload(bare)
        self.assertIs(payload["retryable"], True)
        self.assertEqual(payload["code"], "unity_instance_unreachable")
        self.assertEqual(payload["category"], "session")

        explicit_false = unity_instance_middleware.PluginDisconnectedError(
            "gone", retryable=False)
        payload_false = _classified_error_payload(explicit_false)
        self.assertIs(payload_false["retryable"], False)


if __name__ == "__main__":
    unittest.main()
