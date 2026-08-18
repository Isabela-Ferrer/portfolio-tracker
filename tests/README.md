# The test suite

```bash
pip3 install -r requirements-dev.txt
pytest
```

236 tests, about 2 seconds, no network, no API key, no real database.

Useful invocations:

```bash
pytest tests/test_flags.py            # one file
pytest -k "early_career"              # one topic, across files
pytest -x                             # stop at the first failure
pytest -vv                            # full names and full diffs
```

---

## The ideas this suite is built on

### 1. The pyramid: many cheap tests, few expensive ones

| Layer | Files | What only it can catch | Cost |
|---|---|---|---|
| Unit | `test_flags`, `test_trend_calculator`, `test_careers_classify`, most of `test_content` | wrong logic, bad edge cases | microseconds |
| Integration | `test_database`, `test_job_diff`, `test_digest`, cache tests | wrong relationships between pieces | milliseconds |
| End-to-end | `test_api` | wrong wiring: routes, serialisation, startup | ~1 second |

The temptation is to write everything end-to-end because it "tests more". It
does, badly. A failing HTTP test tells you something somewhere is broken; a
failing unit test tells you which function and which input. Each layer earns its
place by catching what the layer below cannot see.

### 2. A test asserts one behaviour, and its name says which

`test_an_empty_board_response_does_not_mass_close_a_known_board` needs no
comment. When it goes red at 6pm on a Friday, the name alone tells you whether
the change was intended.

Inside, Arrange / Act / Assert. Set up, do the thing **once**, then check.
Poke-assert-poke-assert means the failure line is not the diagnosis.

### 3. Test behaviour, not implementation

`test_the_same_week_is_never_paid_for_twice` never touches the `ai_cache` table
or checks a cache key. It asserts the promise: *the vendor is not called twice*.
Change the hash, restructure the table, rename the column — it keeps passing.

Tests coupled to implementation are what make people say refactoring is
expensive. It isn't; their tests are.

### 4. Fake at the boundary, never in the middle

| Collaborator | How it is faked | Why |
|---|---|---|
| the network | `httpx.MockTransport`, passed in through the existing `client=` argument | no patching at all — the code already had the seam |
| OpenAI | a small recording fake swapped onto `ai_narrator.client` | the API costs money and is nondeterministic |
| the clock | not faked; tests use `db.days_ago_iso(400)` for relative dates | freezing time is a dependency you do not need if you can pick your inputs |
| SQLite | **not faked** | the behaviour under test *is* the SQL |

That last row is the interesting one. `UNIQUE(company_id, type, url)` is what
makes "this week" mean anything, and `INSERT OR IGNORE` returning nothing when
it ignores is a real bug source. A fake database would agree with whatever
fiction you wrote and prove nothing. Real SQLite in a temp file is fast enough
that there is nothing to buy by faking it.

The `mock_http_client` helper is worth studying: because `careers.fetch()` and
`content.fetch_many()` accept an optional `client`, the tests hand them a
transport and patch nothing. **Code that lets you pass in its collaborators is
code you can test without magic.** When testing something feels like it needs
five `monkeypatch` calls, that is a design signal, not a testing problem.

### 5. Isolation, enforced rather than remembered

Every test gets a fresh SQLite file in a fresh `tmp_path`. `monkeypatch` puts
everything back afterwards even when the test fails.

`_never_the_real_database` in `conftest.py` is `autouse=True`, so it applies to
every test whether it asks or not. Autouse is usually a smell — invisible setup
is hard to reason about — but it is right for a safety interlock. Without it, a
test that exercises `ai_narrator._chat` and forgets to request `temp_db` would
silently read and write your actual `data/tracker.db`, and pass or fail
depending on what happened to be in it.

**Opt-in safety is a bug waiting for a distracted afternoon. Opt-out safety is a
bug you have to write on purpose.**

`test_each_test_gets_a_clean_database` asserts the property directly, because
when isolation breaks, twelve unrelated tests fail with twelve confusing
messages, and one clear failure beats that every time.

### 6. Parametrize instead of looping

```python
@pytest.mark.parametrize("delta, expected", [(2, False), (3, True), (-3, True)])
def test_hiring_shift_ignores_headcount_noise_below_the_threshold(delta, expected):
```

Three independent tests, three names in the output, three separate pass/fail
results. A `for` loop over the same cases stops at the first failure and hides
the rest.

### 7. Test the boundary, not the middle

`HEADCOUNT_NOISE = 3` with `abs(delta) >= 3`. The cases that matter are 2 (last
value that must not fire), 3 (first that must), and −3 (proving `abs()` is
really there). Testing 0 and 50 proves almost nothing: off-by-one is the most
common bug in the language and it only ever lives at the edge.

### 8. Every fixed bug becomes a test

`CLAUDE.md` is full of sentences of the form "this used to happen". Each one is
now a test:

| The bug | The test |
|---|---|
| "262 new roles this week" on a first crawl | `test_the_first_crawl_records_the_board_without_announcing_it` |
| an ATS blip read as a mass layoff | `test_an_empty_board_response_does_not_mass_close_a_known_board` |
| undated posts dated the morning of the crawl | `test_an_undated_signal_stays_undated`, `test_a_page_with_no_date_returns_an_empty_string`, `test_an_undated_item_shows_no_date` |
| "Internal Tools Engineer" reported as an internship | `test_senior_and_lookalike_titles_are_not_early_career` |
| the model narrating the dashboard back at you | `test_bullets_that_restate_the_dashboard_are_dropped_from_the_output` |
| the same five internships re-listed every Monday | `test_standing_early_career_roles_are_listed_once_at_the_bottom` |
| a subject line announcing news in a week with none | `test_subject_says_nothing_happened_when_nothing_happened` |
| one blog post rendered as three bullets | `test_one_item_never_becomes_three_bullets` |
| "262 headcount" for a board of 262 open reqs | `test_the_hiring_line_reports_open_roles_not_headcount` |

The workflow, when something breaks in production: **write the failing test
first.** It proves you have actually reproduced the bug before you start
guessing at fixes, and it stays afterwards as the proof it will not come back.
A bug fixed without a test is a bug scheduled for reintroduction.

### 9. Testing code that calls a language model

The usual objection — "the output is nondeterministic, there is nothing to
assert" — aims at the wrong target. **Do not test the model. Test everything you
built around it.** All of this is deterministic and all of it has been wrong:

- the guard that decides whether to call the API *at all*
- the cache that decides whether to call it *again*
- the filter that drops bullets restating the dashboard
- the sanitiser that strips em dashes the prompt already forbade
- the fallback when the call fails or returns broken JSON

`test_a_week_with_no_content_never_calls_the_model` asserts
`fake_openai.calls == []`. That is a claim about a *non-event*, which no return
value could ever express — it needs a fake that records. Fifteen companies,
mostly quiet, is where the entire bill lives.

`test_summaries_use_the_scraped_article_body_not_the_headline` asserts on the
prompt that was sent. Sometimes the interesting question is not what came back
but what you asked.

### 10. Assert on content, never on chrome

`test_digest.py` renders real HTML and asserts things like `"Anthropic" in body`
and `"localhost" not in body`. Never `padding:14px`. Restyle the email freely
and these keep passing; change what information reaches Isa and they fail.

That line — content versus presentation — is what stops a UI suite from becoming
the thing everyone disables.

### 11. Both halves of every filter

`_is_dashboard_fact` gets a table of strings it must catch **and** a table it
must let through. "Raised a 200 million dollar Series B" has a number next to a
noun; a slightly greedier regex eats it and the digest silently drops the most
interesting thing that happened all week.

Precision tests and recall tests catch opposite mistakes. One without the other
is half a test.

### 12. Test the empty case, the None case, and the degenerate case

They are the least fun to write and where the crashes are. Every parametrized
table here ends with `""` and `None`, because third-party JSON supplies both
constantly. `test_a_completely_empty_week_still_renders_a_valid_email` covers
the state a real user hits over a holiday — and a crash there means no email at
all, which is worse than a boring one.

---

## What this suite deliberately does not test

Knowing what to leave alone matters as much as coverage.

- **The prose the model writes.** Not assertable, not our bug.
- **Live ATS payloads.** The fixtures are trimmed copies of real responses.
  Testing against the live API makes the suite fail when Greenhouse deploys.
- **The scheduler's timing.** Testing that something fires in seven days means
  either a seven-day test or a reimplementation of the clock. Low value.
- **Exact HTML/CSS.** See §10.
- **`discovery.py`'s full crawl.** Its pure parts (`slug_candidates`,
  `_name_matches`, `_normalise`) are covered; the fan-out across three ATS
  platforms and five URL paths is mostly network orchestration, and a test of it
  would mostly be a test of the fake.

## Is the suite any good? A way to check

Coverage percentage measures which lines *ran*, not which behaviours are
*pinned*. A test with no assertions gives you 100% coverage of everything it
touches.

The honest check is to break the code on purpose and see whether anything goes
red. Five deliberate mutations, each caught by exactly one intended test:

| Mutation | Caught by |
|---|---|
| `HEADCOUNT_NOISE = 3` → `2` | `test_hiring_shift_ignores_headcount_noise_below_the_threshold[-2-False]` |
| drop the `\b` anchors in `EARLY_CAREER_RE` | `test_senior_and_lookalike_titles_are_not_early_career[Head of Internal Communications]` |
| `if not postings and existing:` → always | `test_an_empty_board_response_does_not_mass_close_a_known_board` |
| `s.published_at or ""` → `or created` | `test_an_undated_signal_stays_undated` |
| remove the `_is_dashboard_fact` filter | `test_bullets_that_restate_the_dashboard_are_dropped_from_the_output` |

Do this occasionally with a change you are about to make anyway. If nothing
fails, you have found a gap.

## Adding a test

1. **Where does it belong?** Pure function → a unit file. Needs the database or
   two calls in sequence → an integration file. Only observable through HTTP →
   `test_api.py`. When in doubt, the lowest layer that can see the bug.
2. **Name it as a sentence.** `test_<subject>_<does_what>_<when>`.
3. **Use the factories** in `conftest.py` and override only the field the test
   is about. Everything else is noise.
4. **One action, then assert.**
5. **Watch it fail first.** A test you have never seen fail is a test you have
   not tested. Break the code, confirm red, restore, confirm green.
