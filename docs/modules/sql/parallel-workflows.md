[SQL module index](index.md)

# Parallel Workflows

Parallel workflows group independent SQL tasks and run them with controlled
concurrency. They are useful for fan-out reads, independent refreshes, and
multi-step jobs where some steps can proceed while others wait on database I/O.

## Task Batches

Task specs describe the operation type and the same arguments that would be
passed to the matching synchronous helper. Named tasks make result dictionaries
stable and easier to inspect.

Use fail-fast behavior when one failed task invalidates the batch. Disable it
when partial results are acceptable and failures should be reported per task.

## Concurrency Caps

Requested concurrency describes how much work the caller wants. Soft caps can
lower actual worker execution without changing the requested batch shape. Hard
caps protect the process from accidentally launching too many workers.

## Pipelines

Custom pipelines keep ordered Python steps inside one task while other tasks
continue under the outer concurrency limit. Use them for small conditional
flows, such as reading a source count before deciding whether to transfer data.

[SQL module index](index.md)
