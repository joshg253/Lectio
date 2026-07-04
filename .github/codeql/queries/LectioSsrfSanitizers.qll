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
