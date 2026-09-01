# ZhiPi — Teacher-Controlled Multimodal Homework Grading and Error-Cause Diagnosis

*English summary. The full documentation is in Chinese: [README.md](README.md). The detailed design lives in [DESIGN.md](DESIGN.md).*

Competition entry materials for the Seewo "ZhiJiao π" track. The system grades handwritten science homework from images, diagnoses the underlying error causes rather than only scoring, and keeps a teacher in final control of every result.

## What it does

- **Image-to-grading pipeline** — preprocessing, OCR / multimodal reading, per-question grading against a rubric, and an evidence chain linking each judgment back to the region of the image it came from.
- **Error-cause diagnosis** — maps a wrong answer to a labeled cause, not just a score, so the teacher sees *why* a class is failing a topic.
- **Confidence routing** — low-confidence results are escalated for teacher review instead of being reported as final.
- **Teacher final review** — the teacher approves or corrects every result; the system never publishes a grade unilaterally.
- **Learning-status reporting** — class-level and per-student views derived from labeled error causes.
- **Feishu integration** — interactive review cards, multi-dimensional table records, and knowledge-base export. Runs in preview mode without credentials, live mode with them, and degrades to preview automatically on any error.

## Repository contents

This is a **materials repository**, not a deployable product. It contains the proposal report, detailed design, industry research, technical documentation, grading samples, and a runnable demo. `DESIGN.md` is the primary document; the README is an entry-point overview.

See the Chinese [README.md](README.md) for the full section list, integration status table, team composition, and compliance statement.

## Data and privacy

- Class learning-status data under `docs/05` is **simulated**, used only to demonstrate the analysis and reporting shape.
- Demo handwriting samples are **programmatically synthesized** (`demo/tools/gen_sample_images.py`); no real student information is included.
- The design follows China's Personal Information Protection Law, the Regulations on Network Protection of Minors, and the Interim Measures for Generative AI Services, and prefers self-hostable domestic models.

## Usage boundary

No code license is attached yet: some material under `samples/`, `design-mock/`, and `team/` involves third-party content and team-member information whose redistribution scope has not been cleared item by item. Reading, review, and academic reference are welcome; redistribution, commercial use, or derivative products need prior contact. See the Chinese README's usage-boundary section for details.
