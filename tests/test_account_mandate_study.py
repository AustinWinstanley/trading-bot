from backtest.account_mandate_study import solve_profile


def test_study_exposes_profile_solver():
    assert callable(solve_profile)
