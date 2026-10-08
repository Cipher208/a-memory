# Model licensing

Applies to the optional spaCy NER models used by the privacy gate
(`hooks` ingest → `mcp_server/utils/privacy.py`) and the graph miners. Read this
before changing how they are distributed, and before answering a licensing
question about them.

## What we do

We do **not** redistribute the models. `a-memory` depends on `spacy` (MIT), which
is normal and unrestricted, and the models are installed separately by the user
through `a-memory-ner`, straight from Explosion's own release URLs. The URLs live
in `mcp_server/utils/ner_models.py`; nothing of the models themselves is vendored,
copied into the wheel, or shipped in the sdist.

Stated reason: the models are not on PyPI (`en-core-web-sm` 404s;
`ru-core-news-sm` resolves to an unrelated `999.9.9` placeholder), and PyPI rejects
any distribution whose metadata carries a direct URL — which is how 1.11.0 failed
its upload. But that is a packaging constraint, not the licensing argument. The
licensing argument is that a user fetching a wheel from its publisher is the
normal path, and it keeps us out of the redistribution question entirely.

## What the licenses actually say

Verified from the installed `METADATA` and `LICENSES_SOURCES`, not from memory.

**Model code — MIT.** Both `en_core_web_sm` and `ru_core_news_sm` 3.8.0 ship
`License: MIT`, `Copyright 2021 ExplosionAI GmbH`. Commercial use, modification
and redistribution of the model **code** are permitted with attribution.

**Training data is where the restrictions are, and they differ per model.** This
is the part that a bare `License: MIT` in `METADATA` hides:

| Model | Source | License as declared |
|---|---|---|
| `en_core_web_sm` | OntoNotes 5 | **`commercial (licensed by Explosion)`** |
| `en_core_web_sm` | ClearNLP dependency conversion | citation only, no code packaged |
| `en_core_web_sm` | WordNet 3.0 | WordNet 3.0 License |
| `ru_core_news_sm` | Nerus | **MIT** |

So the English model carries an obligation Explosion acquired and paid for, and
the Russian model does not. That difference is why "the models are MIT" is not a
safe sentence to write.

## The rule that follows

**A redistributor of `en_core_web_sm` inherits an obligation we have no way to
satisfy.** We are not one, and we do not intend to become one:

* never vendor a model wheel into the repository, the wheel or the sdist;
* never re-upload a model to an index under our name;
* `a-memory-ner` installs **from Explosion's URLs** — do not replace them with a
  mirror of ours, which would make us the distributor.

This is the conservative reading, and the cost of it is zero: the user runs one
command that we document. When in doubt, do not redistribute.

**Not legal advice.** The primary sources are in every install:
`<model>.dist-info/LICENSE` and `LICENSES_SOURCES`. For a commercial deployment
that depends on redistributing a model, ask a lawyer, not a README.
