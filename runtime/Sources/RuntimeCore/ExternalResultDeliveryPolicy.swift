/// Trusted host adapter policy for the one external result. Source publication,
/// owning joins and backend/IO closure keep their independent original duties.
package enum ExternalResultDeliveryPolicy: String, Sendable {
    case afterBackendClose = "after-backend-close"
    case afterSourceJoin = "after-source-join"
}
