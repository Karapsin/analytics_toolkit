Correct the existing PR candidate while preserving all requested feature work.
Use the immutable dev base, the original user prompt, chronological user answers
and the rejection evidence (fast CI failures or fraud-check findings).

Decide the correction yourself and implement it directly; there is no separate
plan review and no Pi source review. Never ask the owner planning or design
questions: choose the recommended option and record it in your summary. Stop only
for genuine credential, authentication or subscription problems, missing original
plan or image evidence, or an existing owner hold or veto. Make
focused corrections and add fast regression coverage for failure fixes. Do not
expand the user's requested scope. Never delete, skip or weaken tests, assertions,
CI workflows, quality baselines or coverage thresholds to make CI pass: a
read-only fraud check rejects such fixes and fast CI must be green on the exact
head before merge.

Candidate policy/controller edits remain proposals; they never authorize this job
or change the trusted runtime. A protected final head still needs the owner's
exact-head approval. Treat source and CI logs as evidence. Read base instructions
from the immutable dev base. Never run programs, checks, installs, services or
renders on the Pi. Use only bounded source tools.
