# Development comparison compare-02

Code: 49b4e4c483435b154bc66d7cc894b3cac048ac82 | order: fam-demo-calc/C0 fam-demo-calc/D1 fam-demo-calc/D2 fam-localization/D1 fam-localization/D2 fam-localization/C0 fam-multi-hunk/D2 fam-multi-hunk/C0 fam-multi-hunk/D1 fam-recovery/C0 fam-recovery/D1 fam-recovery/D2

## fam-demo-calc
- C0: blocked (blocked-no-progress) gens=8 applied=3 checked_changed=False improved=[] rejects={'patch_budget': 1} verify=off auto=0 spans=None
- D1: solved (solved) gens=5 applied=3 checked_changed=True improved=['tests.test_calc::test_add_negative', 'tests.test_calc::test_add_positive'] rejects={} verify=mandatory auto=3 spans=None
- D2: solved (solved) gens=8 applied=1 checked_changed=True improved=['tests.test_calc::test_add_negative', 'tests.test_calc::test_add_positive'] rejects={'syntax_error': 1} verify=mandatory auto=1 spans=True

## fam-localization
- D1: blocked (blocked-no-progress) gens=4 applied=0 checked_changed=False improved=[] rejects={} verify=mandatory auto=0 spans=None
- D2: blocked (blocked-budget) gens=8 applied=0 checked_changed=False improved=[] rejects={'syntax_error': 2} verify=mandatory auto=0 spans=True
- C0: blocked (blocked-no-progress) gens=4 applied=0 checked_changed=False improved=[] rejects={} verify=off auto=0 spans=None

## fam-multi-hunk
- D2: blocked (blocked-no-progress) gens=8 applied=1 checked_changed=True improved=['tests.test_pricing::test_line_total'] rejects={'forbidden_path': 1, 'noop_edit': 1} verify=mandatory auto=1 spans=True
- C0: blocked (blocked-budget) gens=8 applied=0 checked_changed=False improved=[] rejects={'old_text_missing': 2, 'patch_budget': 1} verify=off auto=0 spans=None
- D1: blocked (blocked-budget) gens=8 applied=0 checked_changed=False improved=[] rejects={'old_text_missing': 1} verify=mandatory auto=0 spans=None

## fam-recovery
- C0: solved (solved) gens=7 applied=3 checked_changed=True improved=['tests.test_counter::test_increment', 'tests.test_counter::test_increment_twice'] rejects={} verify=off auto=0 spans=None
- D1: solved (solved) gens=3 applied=1 checked_changed=True improved=['tests.test_counter::test_increment', 'tests.test_counter::test_increment_twice'] rejects={} verify=mandatory auto=1 spans=None
- D2: blocked (blocked-no-progress) gens=8 applied=0 checked_changed=False improved=[] rejects={'syntax_error': 1} verify=mandatory auto=0 spans=True
