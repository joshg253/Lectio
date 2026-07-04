/**
 * Lectio-specific path-injection sanitizer barrier.
 *
 * Subclasses the abstract `PathInjection::Sanitizer` from the standard
 * path-injection query. Any subclass in the import closure is used as a barrier
 * by the stock taint config.
 *
 * Guard modeled (services/__init__.py): `assert_safe_feed_id(feed_id)` raises
 * unless `feed_id` is a UUID, so once it has been called on a value that value
 * is safe to interpolate into a filesystem path. The flagged sinks build
 * `_dir()/f"{feed_id}.xml"` in scraper_service.py and deviantart.py right after
 * this assertion.
 *
 * We key the barrier on the SSA variable: any use of the same SSA definition
 * that `assert_safe_feed_id` was called on is treated as validated. SSA
 * guarantees it is the identical value, and the assertion raises for anything
 * that is not a UUID, so all reached uses are safe.
 */

import python
import semmle.python.ApiGraphs
import semmle.python.dataflow.new.DataFlow
import semmle.python.security.dataflow.PathInjectionCustomizations

/** A call to `services.assert_safe_feed_id(...)`. */
private API::CallNode assertSafeFeedIdCall() {
  result = API::moduleImport("services").getMember("assert_safe_feed_id").getACall()
  or
  result = API::moduleImport("services.assert_safe_feed_id").getACall()
}

/** Uses of an SSA variable that has been validated by `assert_safe_feed_id`. */
class ValidatedFeedIdSanitizer extends PathInjection::Sanitizer {
  ValidatedFeedIdSanitizer() {
    exists(EssaVariable v |
      assertSafeFeedIdCall().getArg(0).asCfgNode() = v.getAUse() and
      this.asCfgNode() = v.getAUse()
    )
  }
}
