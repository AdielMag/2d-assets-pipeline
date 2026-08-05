from app.models import Asset, Project
from app.prompting import compose_prompt, compose_sections, external_prompt


def test_compose_prompt_normal():
    project = Project(id=1, name="Test Project", style_description="Hand-drawn fantasy", palette=["#ff0000", "#00ff00"])
    asset = Asset(id=1, project_id=1, name="Test Asset", type="ui_element", prompt="Golden key icon", aspect_ratio="1:1", resolution="256x256", override_entire_prompt=False)
    
    sections = compose_sections(project, asset.type, asset.prompt, asset.aspect_ratio, asset.resolution, override_entire_prompt=False)
    assert sections["style"] == "Hand-drawn fantasy"
    assert "SYMMETRIC" in sections["rules"]
    assert sections["user"] == "Golden key icon"
    assert sections["override_entire_prompt"] is False

    prompt = compose_prompt(project, asset)
    assert "Art style: Hand-drawn fantasy" in prompt
    assert "Golden key icon" in prompt


def test_compose_prompt_override():
    project = Project(id=1, name="Test Project", style_description="Hand-drawn fantasy", palette=["#ff0000", "#00ff00"])
    asset = Asset(id=1, project_id=1, name="Test Asset", type="ui_element", prompt="Golden key icon", aspect_ratio="1:1", resolution="256x256", override_entire_prompt=True)
    
    sections = compose_sections(project, asset.type, asset.prompt, asset.aspect_ratio, asset.resolution, override_entire_prompt=True)
    assert sections["style"] == ""
    assert sections["rules"] == ""
    assert sections["user"] == "Golden key icon"
    assert sections["override_entire_prompt"] is True

    # Composed prompt when override_entire_prompt=True should strictly equal user prompt
    prompt = compose_prompt(project, asset)
    assert prompt == "Golden key icon"

    # Per-generation override
    asset_normal = Asset(id=2, project_id=1, name="Test Asset 2", type="ui_element", prompt="Silver coin", override_entire_prompt=False)
    prompt_gen_override = compose_prompt(project, asset_normal, override_entire_prompt=True)
    assert prompt_gen_override == "Silver coin"
