"""Phase 6: the evaluation harness.

Replays recorded coding sessions — optionally with a synthetically injected
bug — through the real pipeline (`shadowci.pipeline`, the same module
`watch` and `radar` drive) on a simulated clock, and scores the replay
against ground truth: detection latency, precision, accept rate, and false
interruptions. This is a third driver over the pipeline, not a mock of it.
"""
