Added the optional 3-scale enterprise grading to every reporting surface: a
per-trial "Enterprise Score" headline row and "Enterprise by Category" table,
a cross-trial "Enterprise Grading (0–3 rubric)" section with the per-category
breakdown (or a "not configured" note when no scenario opts in), an
`Enterprise:` line in the CLI final panel, an `enterprise_score` +
`enterprise_categories` JSON payload, and a nullable `enterprise_score` sqlite
column. The 0–2 `final_score` formula is unchanged, so historical results stay
comparable; docs/methodology.md documents the 0–3 rubric.
