# Security policy

Please report credential exposure, unsafe path or subprocess handling, arbitrary
command execution, or dependency-integrity concerns privately to the repository
maintainers before filing a public issue.

The runtime launches a local inference worker as a subprocess and exchanges a
typed permit over stdio. Treat model files, tokenizer data, and recorded
evidence as untrusted input: the reader constrains paths and sizes, but native
decoders and the model runtime remain a native-code surface. Keep model and data
paths outside privileged system directories, and run untrusted workloads in an
isolated account or container. Any local sudo path is never required by a check
and is never stored in this repository.

Only the current default branch is supported during the v0.x phase.
