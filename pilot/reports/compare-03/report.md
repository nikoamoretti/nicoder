# Bounded comparison compare-03

Code: 49183e9e4b911fda54e54181042cd66cc4a24f94 | catalogue: span-catalogue-v2
Order: fam-demo-calc/D1 fam-demo-calc/D2 fam-localization/D2 fam-localization/D1 fam-multi-hunk/D1 fam-multi-hunk/D2 fam-recovery/D2 fam-recovery/D1
Probes: 2/2 | cells: 8/8

- probe D1/check-final-channel: ok (ok)
- probe D2/span-shape: ok (ok)

## fam-demo-calc
- D1: solved (solved) gens=5 applied=3 checks=['pass', 'fail', 'pass'] improved=['tests.test_calc::test_add_negative', 'tests.test_calc::test_add_positive'] rejects={} auto=3 spans=0 unit=None
- D2: solved (solved) gens=3 applied=1 checks=['pass'] improved=['tests.test_calc::test_add_negative', 'tests.test_calc::test_add_positive'] rejects={} auto=1 spans=3 unit=True

## fam-localization
- D2: solved (solved) gens=3 applied=1 checks=['pass'] improved=['tests.test_session::test_refresh_preserves_redirect'] rejects={} auto=1 spans=21 unit=True
- D1: blocked (blocked-validation-failed) gens=6 applied=2 checks=['pass', 'pass'] improved=['tests.test_session::test_refresh_preserves_redirect'] rejects={} auto=2 spans=0 unit=None

## fam-multi-hunk
- D1: blocked (blocked-no-progress) gens=5 applied=0 checks=[] improved=[] rejects={'old_text_missing': 1} auto=0 spans=0 unit=None
- D2: blocked (blocked-validation-failed) gens=6 applied=2 checks=['fail', 'fail'] improved=['tests.test_pricing::test_line_total'] rejects={} auto=2 spans=3 unit=True

## fam-recovery
- D2: solved (solved) gens=3 applied=1 checks=['pass'] improved=['tests.test_counter::test_increment', 'tests.test_counter::test_increment_twice'] rejects={} auto=1 spans=12 unit=False
- D1: solved (solved) gens=4 applied=1 checks=['pass'] improved=['tests.test_counter::test_increment', 'tests.test_counter::test_increment_twice'] rejects={} auto=1 spans=0 unit=None
