from xtalk.psych_sop.sop_navigator import SOPNavigator


def test_user_quit_enters_aborted():
    navigator = SOPNavigator.from_yaml()

    action = navigator.step("退出", {})

    assert action.node_id == "ABORTED"
    assert action.action == "abort"
    assert action.should_end is True
