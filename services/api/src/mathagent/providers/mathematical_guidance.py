"""Problem-independent guidance, shared with comparison baselines."""

SOLUTION_SET_GUIDANCE = r"""For existence or find-all-solutions problems, do not assume
that any solution exists. A complete solution set may be empty, finite or infinite.
Consider both constructing admissible solutions and deriving necessary conditions
that could exclude every solution; select routes from mathematical evidence, without
forcing a nonexistence route or preferring an empty answer on every problem.
If proving nonexistence, begin with an arbitrary hypothetical solution satisfying
the original hypotheses and cover the entire allowed domain. A failed construction,
an unsuccessful approach, timeout or exhausted budget does not prove nonexistence.
Finite searches cannot exclude an unbounded domain unless a proved bound makes the
search exhaustive. Do not strengthen assumptions or mistake a contradiction in one
method for a contradiction in the original problem.
A claimed empty solution set is a mathematical candidate, written \boxed{\varnothing}
with its justification and any remaining proof gaps. It is not abstention. Unknown or
unresolved means no established answer: say so and do not put \varnothing in a box
as a placeholder. Finding solutions also requires proving the claimed set is complete.
When reviewing nonexistence, check domain coverage, quantifiers, necessary versus
sufficient conditions, all exceptional cases and any theorem assumptions; one valid
solution refutes the empty-set claim. Apply the same proof standard to empty and
nonempty solution sets. A remembered theorem or finite experiment alone is not a
certificate. Use LaTeX for mathematical expressions.
"""
