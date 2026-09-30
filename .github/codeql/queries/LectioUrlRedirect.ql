/**
 * @name URL redirection from remote source (Lectio guard-aware)
 * @description A copy of `py/url-redirection` that treats `services.redirects.local_url` as a sanitizer barrier. Replaces the stock
 *              query, which cannot see that the guard confines the target to a same-origin path.
 * @kind path-problem
 * @problem.severity error
 * @security-severity 6.1
 * @precision high
 * @id py/lectio/url-redirection
 * @tags security
 *       external/cwe/cwe-601
 */

import python
import semmle.python.security.dataflow.UrlRedirectQuery
import LectioUrlRedirectSanitizers
import UrlRedirectFlow::PathGraph

from UrlRedirectFlow::PathNode source, UrlRedirectFlow::PathNode sink
where UrlRedirectFlow::flowPath(source, sink)
select sink.getNode(), source, sink, "Untrusted URL redirection depends on a $@.", source.getNode(),
  "user-provided value"
