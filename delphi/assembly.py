from datetime import datetime
from delphi.paths import concept_to_indicator_mapping
from .utils import exists, flatMap
from .types import Delta, Indicator
from typing import *
from indra.statements import Influence, Concept
from fuzzywuzzy import process
from functools import singledispatch, lru_cache
from itertools import permutations
import pandas as pd
import numpy as np
from future.utils import lmap
from scipy.stats import gaussian_kde


def get_valid_statements_for_modeling(sts: List[Influence]) -> List[Influence]:
    """ Select INDRA statements that can be used to construct a Delphi model
    from a given list of statements. """

    return [
        s
        for s in sts
        if is_grounded(s)
        and (s.subj_delta["polarity"] is not None)
        and (s.obj_delta["polarity"] is not None)
    ]


def deltas(s: Influence) -> Tuple[Delta, Delta]:
    return s.subj_delta, s.obj_delta


def get_respdevs(gb):
    return gb["respdev"]


def make_edge(
    sts: List[Influence], p: Tuple[str, str]
) -> Tuple[str, str, Dict[str, List[Influence]]]:
    edge = (
        p[0],
        p[1],
        {
            "InfluenceStatements": [
                s for s in sts if (p[0], p[1]) == nameTuple(s)
            ]
        },
    )
    return edge


def top_grounding(c: Concept, ontology="UN") -> str:
    return (
        c.db_refs[ontology][0][0].split("/")[-1]
        if ontology in c.db_refs
        else c.name
    )


def top_grounding_score(c: Concept, ontology: str = "UN") -> float:
    return c.db_refs[ontology][0][1]


def nameTuple(s: Influence) -> Tuple[str, str]:
    return top_grounding(s.subj), top_grounding(s.obj)


def get_concepts(sts: List[Influence]) -> Set[str]:
    return set(flatMap(nameTuple, sts))


def process_concept_name(name: str) -> str:
    return name.replace("_", " ")


def filter_statements(sts: List[Influence]) -> List[Influence]:
    return [s for s in sts if is_well_grounded(s) and is_simulable(s)]


def constructConditionalPDF(
    gb, rs: np.ndarray, e: Tuple[str, str, Dict]
) -> gaussian_kde:

    sts = e[2]["InfluenceStatements"]

    # Make a adjective-response dict.

    def get_adjective(d: Delta) -> Optional[str]:
        """ Get the first adjective from subj_delta or obj_delta """

        if isinstance(d["adjectives"], list):
            if d["adjectives"]:
                adj = d["adjectives"][0]
            else:
                adj = None
        else:
            adj = d["adjectives"]

        return adj if adj in gb.groups.keys() else None

    adjectiveResponses = {
        a: get_respdevs(gb.get_group(a))
        for a in set(
            filter(
                exists, flatMap(lambda s: lmap(get_adjective, deltas(s)), sts)
            )
        )
    }

    def responses(adj: Optional[str]) -> np.ndarray:
        return adjectiveResponses[adj] if exists(adj) else rs

    rs_subj = []
    rs_obj = []

    for s in sts:
        rs_subj.append(
            s.subj_delta["polarity"]
            * np.array(responses(get_adjective(s.subj_delta)))
        )
        rs_obj.append(
            s.obj_delta["polarity"]
            * np.array(responses(get_adjective(s.obj_delta)))
        )

    rs_subj = np.concatenate(rs_subj)
    rs_obj = np.concatenate(rs_obj)

    xs1, ys1 = np.meshgrid(rs_subj, rs_obj, indexing="xy")

    if (
        len(
            [
                s
                for s in sts
                if s.subj_delta["polarity"] == s.obj_delta["polarity"]
            ]
        )
        == 1
    ):

        xs2, ys2 = -xs1, -ys1
        thetas = np.append(
            np.arctan2(ys1.flatten(), xs1.flatten()),
            np.arctan2(ys2.flatten(), xs2.flatten()),
        )
    else:
        thetas = np.arctan2(ys1.flatten(), xs1.flatten())

    return gaussian_kde(thetas)


def is_simulable(s: Influence) -> bool:
    return all(map(exists, map(lambda x: x["polarity"], deltas(s))))


@singledispatch
def is_grounded(arg):
    """ Generic function to check grounding """
    pass


@is_grounded.register(Concept)
def _(c: Concept, ontology: str = "UN"):
    """ Check if a concept is grounded """
    return (
        ontology in c.db_refs
        and c.db_refs[ontology][0][0].split('/')[1] != "properties"
    )


@is_grounded.register(Influence)
def _(s: Influence, ontology: str = "UN"):
    """ Check if an Influence statement is grounded """
    return is_grounded(s.subj) and is_grounded(s.obj)


@singledispatch
def is_well_grounded():
    pass


@is_well_grounded.register(Concept)
def _(c: Concept, ontology: str = "UN", cutoff: float = 0.7) -> bool:

    return is_grounded(c, ontology) and (
        top_grounding_score(c, ontology) >= cutoff
    )


@lru_cache(maxsize=32)
@is_well_grounded.register(Influence)
def _(s: Influence, ontology: str = "UN", cutoff: float = 0.7) -> bool:
    """ Returns true if both subj and obj are grounded to the specified
    ontology"""

    return all(
        map(lambda c: is_well_grounded(c, ontology, cutoff), s.agent_list())
    )


def is_grounded_to_name(c: Concept, name: str, cutoff=0.7) -> bool:
    return (
        (top_grounding(c) == name)
        if is_well_grounded(c, "UN", cutoff)
        else False
    )


def contains_concept(s: Influence, concept_name: str, cutoff=0.7) -> bool:
    return any(
        map(
            lambda c: is_grounded_to_name(c, concept_name, cutoff),
            s.agent_list(),
        )
    )


def contains_relevant_concept(
    s: Influence, relevant_concepts: List[str], cutoff=0.7
) -> bool:
    return any(
        map(lambda c: contains_concept(s, c, cutoff=cutoff), relevant_concepts)
    )


def get_best_match(indicator: Indicator, items: Iterable[str]) -> str:
    best_match = process.extractOne(indicator.name, items)[0]
    return best_match


def get_data(filename: str) -> pd.DataFrame:
    df = pd.read_csv(filename, sep="|", index_col="Indicator Name")
    return df


def get_mean_precipitation(year: int, cycles_output='weather.dat'):
    df = pd.read_table(cycles_output)
    df.columns = df.columns.str.strip()
    df.columns = [c + f" ({df.iloc[0][c].strip()})" for c in df.columns]
    df.drop([0], axis=0, inplace=True)
    df['DATE (YYYY-MM-DD)'] = pd.to_datetime(df['DATE (YYYY-MM-DD)'], format='%Y-%m-%d')
    return (df.loc[(datetime(year, 1, 1) < df['DATE (YYYY-MM-DD)']) 
                 & (df['DATE (YYYY-MM-DD)'] < datetime(year, 12, 31))]
            ['PRECIPITATION (mm)'].values.astype(float).mean())

def get_indicator_value(
    indicator: Indicator, date: datetime, df: pd.DataFrame
) -> Optional[float]:

    if indicator.source == 'FAO/WDI':
        best_match = get_best_match(indicator, df.index)

        year = str(date.year)
        if not year in df.columns:
            return None
        else:
            indicator_value = df[year][best_match]
            indicator_units = df.loc[best_match]['Unit']

        return ((indicator_value, indicator_units)
                if not pd.isna(indicator_value) else (None, indicator_units))

    elif indicator.source == 'CYCLES':
        return get_mean_precipitation(date.year), 'mm'


def process_variable_name(x: str):
    xs = x.replace("\/", "|").split("/")
    xs = [x.replace("|", "/") for x in xs]
    xs.reverse()
    return " ".join(xs[0:2])


def construct_concept_to_indicator_mapping(n: int = 2) -> Dict[str, List[str]]:
    """ Create a dictionary mapping high-level concepts to low-level indicators """

    df = pd.read_table(
        concept_to_indicator_mapping,
        usecols=[1, 3, 4],
        names=["Concept Grounding", "Indicator Grounding", "Score"],
    )
    gb = df.groupby("Concept Grounding")

    construct_variable_name = (
        lambda x: x.split("/")[-1] + " " + x.split("/")[-2]
    )
    return {
        k.split("/")[-1]: [
            process_variable_name(x)
            for x in v["Indicator Grounding"].values[0:n]
        ]
        for k, v in gb
    }


def get_indicators(concept: str, mapping: Dict = None) -> Optional[List[str]]:
    return (
        [Indicator(x, 'FAO/WDI') for x in mapping[concept]]
        if concept in mapping
        else None
    )