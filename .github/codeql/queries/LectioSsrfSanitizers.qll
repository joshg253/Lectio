/**
 * Lectio-specific SSRF sanitizer barriers.
 *
 * These subclass the abstract `ServerSideRequestForgery::Sanitizer` from the
 * standard SSRF query. Any subclass in the import closure is automatically used
 * as a barrier by the stock taint config, so importing this file into a copy of
 * the SSRF query clears the paths our audited guards already protect.
 *
 * Guards modeled (services/url_guard.py):
 *   - `safe_get` / `safe_head` / `safe_get_async` re-validate the (initial and
 *     every redirect) target with `is_safe_outbound_url` before each request and
 *     raise `UnsafeURLError` otherwise. The flagged sink is the `client.get(...)`
 *     *inside* these wrappers, so we treat every node inside them as sanitized.
 *   - `ensure_safe_outbound_url(x)` returns `x` only when it is safe (raising
 *     otherwise), so its return value is sanitized.
 *   - `is_safe_outbound_url(x)` called directly as a guard clause (the common
 *     shape outside url_guard.py itself: `if not is_safe_outbound_url(x): return`),
 *     e.g. page_fetch.py's urllib TLS-fingerprint fallback, which doesn't go
 *     through the `safe_*` wrappers because it needs a different HTTP client.
 *     ~25 call sites use this exact idiom across the codebase, so it is modeled
 *     as a barrier guard rather than dismissed per call site.
 */

import python
import semmle.python.ApiGraphs
import semmle.python.dataflow.new.DataFlow
import semmle.python.security.dataflow.ServerSideRequestForgeryCustomizations

/** The `services.url_guard` module, however it is imported. */
private API::Node urlGuardModule() {
  result = API::moduleImport("services.url_guard")
  or
  result = API::moduleImport("services").getMember("url_guard")
}

/**
 * Nodes inside the audited outbound-fetch wrappers in `url_guard.py`. Each
 * wrapper validates the target with `is_safe_outbound_url` before every request,
 * so the request sink they contain is not attacker-controlled in practice.
 */
class InsideOutboundWrapperSanitizer extends ServerSideRequestForgery::Sanitizer {
  InsideOutboundWrapperSanitizer() {
    exists(Function f |
      f.getName() in ["safe_get", "safe_head", "safe_get_async"] and
      f.getLocation().getFile().getBaseName() = "url_guard.py" and
      this.getScope() = f
    )
  }
}

/** The validated URL returned by `ensure_safe_outbound_url(...)`. */
class EnsureSafeOutboundUrlSanitizer extends ServerSideRequestForgery::Sanitizer {
  EnsureSafeOutboundUrlSanitizer() {
    this = urlGuardModule().getMember("ensure_safe_outbound_url").getACall()
  }
}

/**
 * The argument to `is_safe_outbound_url(x)`, on the branch where the call
 * returned true — i.e. `x` after a guard clause like
 * `if not is_safe_outbound_url(x): return` (or the positive form,
 * `if is_safe_outbound_url(x): ...`). Mirrors the stock SSRF query's own
 * `ConstCompareAsSanitizerGuard` / `StringRestrictionSanitizerGuard` shape.
 */
private predicate isSafeOutboundUrlGuard(DataFlow::GuardNode g, ControlFlowNode node, boolean branch) {
  exists(DataFlow::CallCfgNode call, DataFlow::Node arg |
    call = urlGuardModule().getMember("is_safe_outbound_url").getACall() and
    call.asCfgNode() = g and
    arg = [call.getArg(0), call.getArgByName("url")] and
    node = arg.asCfgNode() and
    branch = true
  )
}

class IsSafeOutboundUrlGuardSanitizer extends ServerSideRequestForgery::Sanitizer {
  IsSafeOutboundUrlGuardSanitizer() {
    this = DataFlow::BarrierGuard<isSafeOutboundUrlGuard/3>::getABarrierNode()
  }
}
