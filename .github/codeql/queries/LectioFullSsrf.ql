/**
 * @name Full server-side request forgery (Lectio guard-aware)
 * @description A copy of `py/full-ssrf` that treats Lectio's outbound-URL guards
 *              (services.url_guard) as sanitizer barriers. Replaces the stock
 *              query, which flags the already-guarded `url_guard` fetch wrappers.
 * @kind path-problem
 * @problem.severity error
 * @security-severity 9.1
 * @precision high
 * @id py/lectio/full-ssrf
 * @tags security
 *       external/cwe/cwe-918
 */

import python
import semmle.python.security.dataflow.ServerSideRequestForgeryQuery
import LectioSsrfSanitizers
import FullServerSideRequestForgeryFlow::PathGraph

from
  FullServerSideRequestForgeryFlow::PathNode source, FullServerSideRequestForgeryFlow::PathNode sink,
  Http::Client::Request request
where
  fullyControlledRequest(request) and
  request = sink.getNode().(Sink).getRequest() and
  FullServerSideRequestForgeryFlow::flowPath(source, sink)
select request, source, sink, "The full URL of this request depends on a $@.", source.getNode(),
  "user-provided value"
