"""skills-agent skills: skill management subcommands."""

import sys


def cmd_skills_list(args) -> int:
    """Execute `skills-agent skills list`."""
    from cli.main import _build_registry

    registry = _build_registry(args.skill_root)
    skills = registry.scan()

    if not skills:
        print("(no skills found)")
        return 0

    # Calculate column widths
    name_w = max(len("NAME"), max(len(m.name) for m in skills))
    src_w = max(len("SOURCE"), max(len(m.source) for m in skills))
    desc_w = max(len("DESCRIPTION"), max(len(m.description) for m in skills))

    header = f"{'NAME':<{name_w}}  {'SOURCE':<{src_w}}  {'DESCRIPTION':<{desc_w}}"
    sep = "-" * len(header)
    print(header)
    print(sep)
    for m in skills:
        print(f"{m.name:<{name_w}}  {m.source:<{src_w}}  {m.description:<{desc_w}}")

    return 0


def cmd_skills_inspect(args) -> int:
    """Execute `skills-agent skills inspect <skill-name>`."""
    from cli.main import _build_registry

    registry = _build_registry(args.skill_root)
    meta = registry.find(args.skill_name)

    if meta is None:
        print(f"Error: skill '{args.skill_name}' not found.", file=sys.stderr)
        return 1

    # Show all frontmatter fields (including control fields — this is for operators)
    data = meta.model_dump(by_alias=False)
    print(f"Skill: {meta.name}")
    print(f"Path:  {meta.skill_path}")
    print()

    for key, value in data.items():
        if key == "skill_path":
            continue  # already shown above
        if isinstance(value, dict):
            print(f"  {key}:")
            for k, v in value.items():
                print(f"    {k}: {v}")
        elif isinstance(value, list):
            if value:
                print(f"  {key}: {value}")
            else:
                print(f"  {key}: []")
        else:
            print(f"  {key}: {value}")

    return 0
