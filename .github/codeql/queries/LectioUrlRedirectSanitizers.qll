/**
 * Lectio-specific URL-redirection sanitizer barriers.
 *
 * Subclasses the abstract `UrlRedirect::Sanitizer` from the standard URL-redirection query; any subclass in the import closure is
 * used as a barrier by the stock taint config.
 *
 * Guard modeled (services/redirects.py): `local_url(x)` returns `x` only when it is a same-origin path (leading `/`, not `//` or
 * `/\`, no control characters) and `/` otherwise, so its return value cannot send the browser off-site.
 */

import python
import semmle.python.ApiGraphs
import semmle.python.dataflow.new.DataFlow
import semmle.python.security.dataflow.UrlRedirectCustomizations

/** The value returned by `services.redirects.local_url(...)`. */
class LocalUrlSanitizer extends UrlRedirect::Sanitizer {
  LocalUrlSanitizer() {
    this = API::moduleImport("services.redirects").getMember("local_url").getACall()
    or
    this =
      API::moduleImport("services").getMember("redirects").getMember("local_url").getACall()
  }

  /** `local_url` clears every flow state: it rejects `//` and `/\\` prefixes itself, backslash-bearing or not. */
  override predicate sanitizes(UrlRedirect::FlowState state) { any() }
}
