/**
 * @name Uncontrolled data used in path expression (Lectio guard-aware)
 * @description A copy of `py/path-injection` that treats Lectio's
 *              `assert_safe_feed_id` UUID validator as a sanitizer barrier.
 *              Replaces the stock query, which flags the already-guarded
 *              scraped/DeviantArt feed-file paths.
 * @kind path-problem
 * @problem.severity error
 * @security-severity 7.5
 * @precision high
 * @id py/lectio/path-injection
 * @tags security
 *       external/cwe/cwe-022
 */

import python
import semmle.python.security.dataflow.PathInjectionQuery
import LectioPathInjectionSanitizers
import PathInjectionFlow::PathGraph

from PathInjectionFlow::PathNode source, PathInjectionFlow::PathNode sink
where PathInjectionFlow::flowPath(source, sink)
select sink.getNode(), source, sink, "This path depends on a $@.", source.getNode(),
  "user-provided value"
