# Mutation checks

Each logic fix has a regression test. To make sure those tests really protect the fix, every fix
was reverted on a scratch copy and the suite was run: **the suite must fail**. All eight reverts
below were caught.

| # | Fix | How it was reverted | Test that fails |
|---|---|---|---|
| M1 | Shortfall and over-settlement are reported separately, on a net basis | one signed sum of the net differences | `test_shortfall_and_excess_are_reported_separately` |
| M2 | Lookback searches the leg that is actually missing (903 or 904) | always search for 904 | `test_individual_rail_resolves_whichever_leg_is_missing` |
| M3 | Dash/ledger merge is strictly one-to-one | removed `validate="one_to_one"` and the occurrence suffix | `test_unrecon_sides_and_remarks` |
| M4 | `Check_Settlement` uses the same predicate as the batch status | net-only comparison | `test_offsetting_gross_and_fee_errors_are_not_hidden` |
| M5 | NaN-safe amount key | cast amounts with `astype("int64")` | every end-to-end test errors with `IntCastingNaNError` |
| M6 | Neighbouring-day match in `Check_Settlement` must agree on the amount | amount comparison replaced by `True` | `test_boundary_batch_with_the_wrong_amount_is_a_mismatch` |
| M7 | The resolved leg is assigned to the right column | always assigned to the fee | `test_individual_rail_resolves_whichever_leg_is_missing` |
| M8 | A boundary artifact requires the amount to agree (batch status) | amount condition replaced by `False` | `test_shortfall_and_excess_are_reported_separately`, `test_boundary_batch_with_the_wrong_amount_is_a_mismatch` |

To repeat one: edit the line described in the "How it was reverted" column, run `pytest`, and
expect a failure.