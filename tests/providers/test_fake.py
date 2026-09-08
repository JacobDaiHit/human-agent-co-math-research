from mathagent.providers.fake import FakeProvider


def test_fake_output_is_stable_across_instances_and_read_set_order():
    first = FakeProvider().generate(
        goal="证明目标", instruction="固定指令", read_set={"a": "1", "b": "2"}
    )
    second = FakeProvider().generate(
        goal="证明目标", instruction="固定指令", read_set={"b": "2", "a": "1"}
    )
    assert first == second


def test_fake_never_claims_real_model_research_or_proof():
    provider = FakeProvider()
    result = provider.generate(goal="所有素数都是奇数")
    assert "测试夹具，未调用真实模型" in result
    assert "不构成证明、审查或研究结论" in result
    assert "证据状态：草稿；尚未验证" in result
    assert provider.simulated
    assert not provider.external_requests


def test_full_inputs_influence_fingerprint_but_output_is_bounded():
    provider = FakeProvider()
    first = provider.generate(goal="甲" * 20_000, instruction="乙" * 20_000)
    second = provider.generate(goal="甲" * 20_000 + "丙", instruction="乙" * 20_000)
    assert first != second
    assert len(first) < 3000
    assert len(second) < 3000


def test_instruction_and_revision_changes_produce_different_fixture():
    provider = FakeProvider()
    original = provider.generate(goal="目标", instruction="路线甲", read_set={"object": "v1"})
    assert original != provider.generate(
        goal="目标", instruction="路线乙", read_set={"object": "v1"}
    )
    assert original != provider.generate(
        goal="目标", instruction="路线甲", read_set={"object": "v2"}
    )
