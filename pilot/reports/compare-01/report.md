# Development comparison compare-01

Code: b2d84fc018ae5320677f38b93e29b79db8e0d71e | order: fam-demo-calc/A fam-demo-calc/B fam-demo-calc/C fam-localization/A fam-localization/B fam-localization/C fam-multi-hunk/A fam-multi-hunk/B fam-multi-hunk/C fam-recovery/A fam-recovery/B fam-recovery/C

## fam-demo-calc
- A: blocked (blocked-budget) gens=8 applied=0 checked_changed=False improved=[] rejects={'malformed_diff': 3}
- B: blocked (blocked-budget) gens=8 applied=0 checked_changed=False improved=[] rejects={'malformed_diff': 2}
- C: solved (solved) gens=8 applied=3 checked_changed=True improved=['tests.test_calc::test_add_negative', 'tests.test_calc::test_add_positive'] rejects={}

## fam-localization
- A: blocked (blocked-budget) gens=8 applied=0 checked_changed=False improved=[] rejects={'malformed_diff': 3}
- B: blocked (blocked-budget) gens=8 applied=0 checked_changed=False improved=[] rejects={'malformed_diff': 3}
- C: solved (solved) gens=8 applied=3 checked_changed=True improved=['tests.test_session::test_refresh_preserves_redirect'] rejects={}

## fam-multi-hunk
- A: blocked (blocked-no-progress) gens=6 applied=0 checked_changed=False improved=[] rejects={}
- B: blocked (blocked-no-progress) gens=5 applied=0 checked_changed=False improved=[] rejects={}
- C: blocked (blocked-budget) gens=8 applied=0 checked_changed=False improved=[] rejects={'old_text_missing': 3}

## fam-recovery
- A: solved (solved) gens=6 applied=1 checked_changed=True improved=['tests.test_counter::test_increment', 'tests.test_counter::test_increment_twice'] rejects={}
- B: solved (solved) gens=6 applied=1 checked_changed=True improved=['tests.test_counter::test_increment', 'tests.test_counter::test_increment_twice'] rejects={}
- C: blocked (blocked-budget) gens=8 applied=2 checked_changed=False improved=[] rejects={}
