# coding: utf-8
# Copyright (c) Pymatgen Development Team.
# Distributed under the terms of the MIT License.


import os
import abc
import warnings

from collections import defaultdict
from math import sqrt

from monty.design_patterns import cached_class
from monty.serialization import loadfn
from monty.json import MSONable

from pymatgen.io.vasp.sets import MITRelaxSet, MPRelaxSet
from pymatgen.core.periodic_table import Element
from pymatgen.analysis.structure_analyzer import oxide_type, sulfide_type


"""
This module implements Compatibility corrections for mixing runs of different
functionals.
"""

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

__author__ = "Shyue Ping Ong, Anubhav Jain, Stephen Dacek, Sai Jayaraman"
__copyright__ = "Copyright 2012, The Materials Project"
__version__ = "1.0"
__maintainer__ = "Shyue Ping Ong"
__email__ = "shyuep@gmail.com"
__date__ = "Mar 19, 2012"


class CompatibilityError(Exception):
    """
    Exception class for Compatibility. Raised by attempting correction
    on incompatible calculation
    """

    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return self.msg


class Correction(metaclass=abc.ABCMeta):
    """
    A Correction class is a pre-defined scheme for correction a computed
    entry based on the type and chemistry of the structure and the
    calculation parameters. All Correction classes must implement a
    correct_entry method.
    """

    @abc.abstractmethod
    def get_correction(self, entry):
        """
        Returns correction for a single entry.

        Args:
            entry: A ComputedEntry object.

        Returns:
            The energy correction to be applied.

        Raises:
            CompatibilityError if entry is not compatible.
        """
        return

    def correct_entry(self, entry):
        """
        Corrects a single entry.

        Args:
            entry: A ComputedEntry object.

        Returns:
            An processed entry.

        Raises:
            CompatibilityError if entry is not compatible.
        """
        corr = self.get_correction(entry)
        entry.correction += corr[0]
        entry.data["correction_error"] = corr[1]
        return entry


class PotcarCorrection(Correction):
    """
    Checks that POTCARs are valid within a pre-defined input set. This
    ensures that calculations performed using different InputSets are not
    compared against each other.

    Entry.parameters must contain a "potcar_symbols" key that is a list of
    all POTCARs used in the run. Again, using the example of an Fe2O3 run
    using Materials Project parameters, this would look like
    entry.parameters["potcar_symbols"] = ['PAW_PBE Fe_pv 06Sep2000',
    'PAW_PBE O 08Apr2002'].

    Args:
        input_set: InputSet object used to generate the runs (used to check
            for correct potcar symbols)

        check_hash (bool): If true, uses the potcar hash to check for valid
            potcars. If false, uses the potcar symbol (Less reliable).
            Defaults to True

    Raises:
        ValueError if entry do not contain "potcar_symbols" key.
        CombatibilityError if wrong potcar symbols
    """

    def __init__(self, input_set, check_hash=False):
        potcar_settings = input_set.CONFIG["POTCAR"]
        if isinstance(list(potcar_settings.values())[-1], dict):
            if check_hash:
                self.valid_potcars = {k: d["hash"] for k, d in potcar_settings.items()}
            else:
                self.valid_potcars = {
                    k: d["symbol"] for k, d in potcar_settings.items()
                }
        else:
            if check_hash:
                raise ValueError(
                    "Cannot check hashes of potcars," " hashes are not set"
                )
            else:
                self.valid_potcars = {k: d for k, d in potcar_settings.items()}

        self.input_set = input_set
        self.check_hash = check_hash

    def get_correction(self, entry):

        if self.check_hash:
            if entry.parameters.get("potcar_spec"):
                psp_settings = set(
                    [d.get("hash") for d in entry.parameters["potcar_spec"] if d]
                )
            else:
                raise ValueError("Cannot check hash " "without potcar_spec field")
        else:
            if entry.parameters.get("potcar_spec"):
                psp_settings = set(
                    [
                        d.get("titel").split()[1]
                        for d in entry.parameters["potcar_spec"]
                        if d
                    ]
                )
            else:
                psp_settings = set(
                    [
                        sym.split()[1]
                        for sym in entry.parameters["potcar_symbols"]
                        if sym
                    ]
                )

        if {
            self.valid_potcars.get(str(el)) for el in entry.composition.elements
        } != psp_settings:
            raise CompatibilityError("Incompatible potcar")
        return 0, 0

    def __str__(self):
        return "{} Potcar Correction".format(self.input_set.__name__)


@cached_class
class MITGasCorrection(Correction):
    """
    Correct energies of diatomic gases to obtain the right formation energies. As 
    of pymatgen XXX, used only by the MITCompatibility class.

    Note that this depends on calculations being run within the same input set.

    References:
        Persson, K. A.; Waldwick, B.; Lazic, P.; Ceder, G. Prediction of Solid-Aqueous
        Equilibria: Scheme to Combine First-Principles Calculations of Solids with
        Experimental Aqueous States. Phys. Rev. B - Condens. Matter Mater. Phys.
        2012, 85 (23), 1–12. https://doi.org/10.1103/PhysRevB.85.235438.
    """

    def __init__(self, config_file, error_file=None):
        c = loadfn(config_file)
        self.name = c["Name"]
        self.cpd_energies = c["GasCorrections"]
        if error_file:
            e = loadfn(error_file)
            self.cpd_errors = e["GasCorrections"]
        else:
            self.cpd_errors = defaultdict(float)

    def get_correction(self, entry):
        comp = entry.composition

        rform = entry.composition.reduced_formula
        if rform in self.cpd_energies:
            correction = (
                self.cpd_energies[rform] * comp.num_atoms - entry.uncorrected_energy
            )
            error = self.cpd_errors[rform] * comp.num_atoms
            return correction, error
        return 0, 0

    def __str__(self):
        return "{} Gas Correction".format(self.name)


@cached_class
class AnionCorrection(Correction):
    """
    Correct anion energies to obtain the right formation energies. Note that
    this depends on calculations being run within the same input set.

    Args:
        config_file: Path to the selected compatibility.yaml config file.
        correct_peroxide: Specify whether peroxide/superoxide/ozonide
            corrections are to be applied or not.
    """

    def __init__(self, config_file, error_file=None, correct_peroxide=True):
        c = loadfn(config_file)
        self.anion_correction = c.get("AnionCorrections", defaultdict(float))
        self.name = c["Name"]
        self.correct_peroxide = correct_peroxide
        if error_file:
            e = loadfn(error_file)
            self.anion_errors = e.get("AnionCorrections", defaultdict(float))
        else:
            self.anion_errors = defaultdict(float)

    def get_correction(self, entry):
        comp = entry.composition

        correction = 0
        error = 0

        # Skip single elements
        if len(comp) == 1:   
            return 0, 0

        # Check for sulfide corrections
        if Element("S") in comp:
            sf_type = "sulfide"
            if entry.data.get("sulfide_type"):
                sf_type = entry.data["sulfide_type"]
            elif hasattr(entry, "structure"):
                sf_type = sulfide_type(entry.structure)
            if sf_type in self.anion_correction:
                correction += self.anion_correction[sf_type] * comp["S"]
                error = sqrt(error ** 2 + (self.anion_errors[sf_type] * comp["S"]) ** 2)

        # Check for oxide, peroxide, superoxide, and ozonide corrections.
        if Element("O") in comp:
            if self.correct_peroxide:
                if entry.data.get("oxide_type"):
                    if entry.data["oxide_type"] in self.anion_correction:
                        ox_corr = self.anion_correction[entry.data["oxide_type"]]
                        correction += ox_corr * comp["O"]
                        ox_error = self.anion_errors[entry.data["oxide_type"]]
                        error = sqrt(error ** 2 + (ox_error * comp["O"]) ** 2)
                    if entry.data["oxide_type"] == "hydroxide":
                        ox_corr = self.anion_correction["oxide"]
                        correction += ox_corr * comp["O"]
                        ox_error = self.anion_errors["oxide"]
                        error = sqrt(error ** 2 + (ox_error * comp["O"]) ** 2)

                elif hasattr(entry, "structure"):
                    ox_type, nbonds = oxide_type(
                        entry.structure, 1.05, return_nbonds=True
                    )
                    if ox_type in self.anion_correction:
                        correction += self.anion_correction[ox_type] * nbonds
                        error = sqrt(
                            error ** 2 + (self.anion_errors[ox_type] * nbonds) ** 2
                        )
                    elif ox_type == "hydroxide":
                        correction += self.anion_correction["oxide"] * comp["O"]
                        error = sqrt(
                            error ** 2 + (self.anion_errors["oxide"] * comp["O"]) ** 2
                        )
                else:
                    warnings.warn(
                        "No structure or oxide_type parameter present. Note "
                        "that peroxide/superoxide corrections are not as "
                        "reliable and relies only on detection of special"
                        "formulas, e.g., Li2O2."
                    )
                    rform = entry.composition.reduced_formula
                    if rform in UCorrection.common_peroxides:
                        correction += self.anion_correction["peroxide"] * comp["O"]
                        error = sqrt(
                            error ** 2
                            + (self.anion_errors["peroxide"] * comp["O"]) ** 2
                        )
                    elif rform in UCorrection.common_superoxides:
                        correction += self.anion_correction["superoxide"] * comp["O"]
                        error = sqrt(
                            error ** 2
                            + (self.anion_errors["superoxide"] * comp["O"]) ** 2
                        )
                    elif rform in UCorrection.ozonides:
                        correction += self.anion_correction["ozonide"] * comp["O"]
                        error = sqrt(
                            error ** 2 + (self.anion_errors["ozonide"] * comp["O"]) ** 2
                        )
                    elif Element("O") in comp.elements and len(comp.elements) > 1:
                        correction += self.anion_correction["oxide"] * comp["O"]
                        error = sqrt(
                            error ** 2 + (self.anion_errors["oxide"] * comp["O"]) ** 2
                        )
            else:
                correction += self.anion_correction["oxide"] * comp["O"]
                error = sqrt(error ** 2 + (self.anion_errors["oxide"] * comp["O"]) ** 2)

        for anion in ["Br", "I", "Se", "Si", "Sb", "Te"]:
            if Element(anion) in comp and anion in self.anion_correction:
                correction += self.anion_correction[anion] * comp[anion]
                error = sqrt(error ** 2 + (self.anion_errors[anion] * comp[anion]) ** 2)
        
        if self.name != 'MIT': # the MIT compatibility set still uses MITGasCorrection
            for gas in ["H", "N", "F", "Cl"]:
                if Element(gas) in comp and gas in self.anion_correction:     
                    correction += self.anion_correction[gas] * comp[gas]
                    error = sqrt(error ** 2 + (self.anion_errors[gas] * comp[gas]) ** 2)

        return correction, error

    def __str__(self):
        return "{} Anion Correction".format(self.name)


@cached_class
class AqueousCorrection(Correction):
    """
    This class implements aqueous phase compound corrections for elements
    and H2O.

    Args:
        config_file: Path to the selected compatibility.yaml config file.
    """

    def __init__(self, config_file, error_file=None):
        c = loadfn(config_file)
        self.cpd_energies = c["AqueousCompoundEnergies"]
        self.name = c["Name"]
        if error_file:
            e = loadfn(error_file)
            self.cpd_errors = e.get("AqueousCompoundEnergies", defaultdict(float))
        else:
            self.cpd_errors = defaultdict(float)

    def get_correction(self, entry):
        comp = entry.composition
        rform = comp.reduced_formula
        cpdenergies = self.cpd_energies
        correction = 0
        error = 0
        if rform in cpdenergies:
            if rform in ["H2", "H2O"]:
                correction = (
                    cpdenergies[rform] * comp.num_atoms
                    - entry.uncorrected_energy
                    - entry.correction
                )
                error = sqrt(
                    error ** 2
                    + (
                        self.cpd_errors[rform] * comp.num_atoms
                        - entry.uncorrected_energy
                        - entry.correction
                    )
                    ** 2
                )
            else:
                correction += cpdenergies[rform] * comp.num_atoms
                error = sqrt(
                    error ** 2 + (self.cpd_errors[rform] * comp.num_atoms) ** 2
                )
        if not rform == "H2O":
            correction += 0.5 * 2.46 * min(comp["H"] / 2.0, comp["O"])
        return correction, error

    def __str__(self):
        return "{} Aqueous Correction".format(self.name)


@cached_class
class UCorrection(Correction):
    """
    This class implements the GGA/GGA+U mixing scheme, which allows mixing of
    entries. Entry.parameters must contain a "hubbards" key which is a dict
    of all non-zero Hubbard U values used in the calculation. For example,
    if you ran a Fe2O3 calculation with Materials Project parameters,
    this would look like entry.parameters["hubbards"] = {"Fe": 5.3}
    If the "hubbards" key is missing, a GGA run is assumed.

    It should be noted that ComputedEntries assimilated using the
    pymatgen.apps.borg package and obtained via the MaterialsProject REST
    interface using the pymatgen.matproj.rest package will automatically have
    these fields populated.

    Args:
        config_file: Path to the selected compatibility.yaml config file.
        input_set: InputSet object (to check for the +U settings)
        compat_type: Two options, GGA or Advanced.  GGA means all GGA+U
            entries are excluded.  Advanced means mixing scheme is
            implemented to make entries compatible with each other,
            but entries which are supposed to be done in GGA+U will have the
            equivalent GGA entries excluded. For example, Fe oxides should
            have a U value under the Advanced scheme. A GGA Fe oxide run
            will therefore be excluded under the scheme.
    """

    common_peroxides = [
        "Li2O2",
        "Na2O2",
        "K2O2",
        "Cs2O2",
        "Rb2O2",
        "BeO2",
        "MgO2",
        "CaO2",
        "SrO2",
        "BaO2",
    ]
    common_superoxides = ["LiO2", "NaO2", "KO2", "RbO2", "CsO2"]
    ozonides = ["LiO3", "NaO3", "KO3", "NaO5"]

    def __init__(self, config_file, input_set, compat_type, error_file=None):
        if compat_type not in ["GGA", "Advanced"]:
            raise CompatibilityError("Invalid compat_type {}".format(compat_type))

        c = loadfn(config_file)

        self.input_set = input_set
        if compat_type == "Advanced":
            self.u_settings = self.input_set.CONFIG["INCAR"]["LDAUU"]
            self.u_corrections = c["Advanced"]["UCorrections"]
        else:
            self.u_settings = {}
            self.u_corrections = {}

        self.name = c["Name"]
        self.compat_type = compat_type

        if error_file:
            e = loadfn(error_file)
            self.u_errors = e["Advanced"]["UCorrections"]
        else:
            self.u_errors = {}

    def get_correction(self, entry):
        if entry.parameters.get("run_type", "GGA") == "HF":
            raise CompatibilityError("Invalid run type")

        calc_u = entry.parameters.get("hubbards", None)
        calc_u = defaultdict(int) if calc_u is None else calc_u
        comp = entry.composition

        elements = sorted(
            [el for el in comp.elements if comp[el] > 0], key=lambda el: el.X
        )
        most_electroneg = elements[-1].symbol
        correction = 0
        error = 0
        ucorr = self.u_corrections.get(most_electroneg, {})
        usettings = self.u_settings.get(most_electroneg, {})
        uerrors = self.u_errors.get(most_electroneg, defaultdict(float))

        for el in comp.elements:
            sym = el.symbol
            # Check for bad U values
            if calc_u.get(sym, 0) != usettings.get(sym, 0):
                raise CompatibilityError(
                    "Invalid U value of %s on %s" % (calc_u.get(sym, 0), sym)
                )
            if sym in ucorr:
                correction += float(ucorr[sym]) * comp[el]
                error = sqrt(error ** 2 + (float(uerrors[sym]) * comp[el]) ** 2)

        return correction, error

    def __str__(self):
        return "{} {} Correction".format(self.name, self.compat_type)


class Compatibility(MSONable):
    """
    The Compatibility class combines a list of corrections to be applied to
    an entry or a set of entries. Note that some of the Corrections have
    interdependencies. For example, PotcarCorrection must always be used
    before any other compatibility. Also, AnionCorrection("MP") must be used
    with PotcarCorrection("MP") (similarly with "MIT"). Typically,
    you should use the specific MaterialsProjectCompatibility and
    MITCompatibility subclasses instead.

    Args:
        corrections: List of corrections to apply.
    """

    def __init__(self, corrections):
        self.corrections = corrections

    def process_entry(self, entry):
        """
        Process a single entry with the chosen Corrections.

        Args:
            entry: A ComputedEntry object.

        Returns:
            An adjusted entry if entry is compatible, otherwise None is
            returned.
        """
        try:
            corrections, errors = self.get_corrections_dict(entry)
        except CompatibilityError:
            return None
        entry.correction = sum(corrections.values())
        tot_error = 0
        for e in errors.values():
            tot_error += e ** 2
        tot_error = sqrt(tot_error)
        if tot_error == 0:
            # if there are no error values available for the corrections applied, set correction_error to not a number
            entry.data["correction_error"] = float("NaN")
        else:
            entry.data["correction_error"] = tot_error
        return entry

    def get_corrections_dict(self, entry):
        """
        Returns the corrections applied to a particular entry.

        Args:
            entry: A ComputedEntry object.

        Returns:
            ({correction_name: value})
        """
        corrections = {}
        errors = {}
        for c in self.corrections:
            val, e = c.get_correction(entry)
            if val != 0:
                corrections[str(c)] = val
                errors[str(c)] = e
        return corrections, errors

    def process_entries(self, entries):
        """
        Process a sequence of entries with the chosen Compatibility scheme.

        Args:
            entries: A sequence of entries.

        Returns:
            An list of adjusted entries.  Entries in the original list which
            are not compatible are excluded.
        """
        return list(filter(None, map(self.process_entry, entries)))

    def get_explanation_dict(self, entry):
        """
        Provides an explanation dict of the corrections that are being applied
        for a given compatibility scheme. Inspired by the "explain" methods
        in many database methodologies.

        Args:
            entry: A ComputedEntry.

        Returns:
            (dict) of the form
            {"Compatibility": "string",
            "Uncorrected_energy": float,
            "Corrected_energy": float,
            "Correction_error:" float,
            "Corrections": [{"Name of Correction": {
            "Value": float, "Explanation": "string"}]}
        """
        centry = self.process_entry(entry)
        if centry is None:
            uncorrected_energy = entry.uncorrected_energy
            corrected_energy = None
            correction_error = None
        else:
            uncorrected_energy = centry.uncorrected_energy
            corrected_energy = centry.energy
            correction_error = centry.data["correction_error"]
        d = {
            "compatibility": self.__class__.__name__,
            "uncorrected_energy": uncorrected_energy,
            "corrected_energy": corrected_energy,
            "correction_error": correction_error,
        }
        corrections = []
        corr_dict, error_dict = self.get_corrections_dict(entry)
        for c in self.corrections:
            cd = {
                "name": str(c),
                "description": c.__doc__.split("Args")[0].strip(),
                "value": corr_dict.get(str(c), 0),
                "error": error_dict.get(str(c), 0),
            }
            corrections.append(cd)
        d["corrections"] = corrections
        return d

    def explain(self, entry):
        """
        Prints an explanation of the corrections that are being applied for a
        given compatibility scheme. Inspired by the "explain" methods in many
        database methodologies.

        Args:
            entry: A ComputedEntry.
        """
        d = self.get_explanation_dict(entry)
        print(
            "The uncorrected value of the energy of %s is %f eV"
            % (entry.composition, d["uncorrected_energy"])
        )
        print(
            "The following corrections / screening are applied for %s:\n"
            % d["compatibility"]
        )
        for c in d["corrections"]:
            print("%s correction: %s\n" % (c["name"], c["description"]))
            print("For the entry, this correction has the value %f eV." % c["value"])
            print("-" * 30)

        print("The final energy after corrections is %f" % d["corrected_energy"])


class MaterialsProjectCompatibility(Compatibility):
    """
    This class implements the GGA/GGA+U mixing scheme, which allows mixing of
    entries. Note that this should only be used for VASP calculations using the
    MaterialsProject parameters (see pymatgen.io.vaspio_set.MPVaspInputSet).
    Using this compatibility scheme on runs with different parameters is not
    valid.

    Args:
        compat_type: Two options, GGA or Advanced.  GGA means all GGA+U
            entries are excluded.  Advanced means mixing scheme is
            implemented to make entries compatible with each other,
            but entries which are supposed to be done in GGA+U will have the
            equivalent GGA entries excluded. For example, Fe oxides should
            have a U value under the Advanced scheme. A GGA Fe oxide run
            will therefore be excluded under the scheme.
        correct_peroxide: Specify whether peroxide/superoxide/ozonide
            corrections are to be applied or not.
        check_potcar_hash (bool): Use potcar hash to verify potcars are correct.
    """

    def __init__(
        self, compat_type="Advanced", correct_peroxide=True, check_potcar_hash=False
    ):
        self.compat_type = compat_type
        self.correct_peroxide = correct_peroxide
        self.check_potcar_hash = check_potcar_hash
        fp = os.path.join(MODULE_DIR, "MPCompatibility.yaml")
        fp_error = os.path.join(MODULE_DIR, "MPCompatibilityErrors.yaml")
        super().__init__(
            [
                PotcarCorrection(MPRelaxSet, check_hash=check_potcar_hash),
                AnionCorrection(
                    fp, error_file=fp_error, correct_peroxide=correct_peroxide
                ),
                UCorrection(fp, MPRelaxSet, compat_type, error_file=fp_error),
            ]
        )


class MITCompatibility(Compatibility):
    """
    This class implements the GGA/GGA+U mixing scheme, which allows mixing of
    entries. Note that this should only be used for VASP calculations using the
    MIT parameters (see pymatgen.io.vaspio_set MITVaspInputSet). Using
    this compatibility scheme on runs with different parameters is not valid.

    Args:
        compat_type: Two options, GGA or Advanced.  GGA means all GGA+U
            entries are excluded.  Advanced means mixing scheme is
            implemented to make entries compatible with each other,
            but entries which are supposed to be done in GGA+U will have the
            equivalent GGA entries excluded. For example, Fe oxides should
            have a U value under the Advanced scheme. A GGA Fe oxide run
            will therefore be excluded under the scheme.
        correct_peroxide: Specify whether peroxide/superoxide/ozonide
            corrections are to be applied or not.
        check_potcar_hash (bool): Use potcar hash to verify potcars are correct.
    """

    def __init__(
        self, compat_type="Advanced", correct_peroxide=True, check_potcar_hash=False
    ):
        self.compat_type = compat_type
        self.correct_peroxide = correct_peroxide
        self.check_potcar_hash = check_potcar_hash
        fp = os.path.join(MODULE_DIR, "MITCompatibility.yaml")
        super().__init__(
            [
                PotcarCorrection(MITRelaxSet, check_hash=check_potcar_hash),
                MITGasCorrection(fp),
                AnionCorrection(fp, correct_peroxide=correct_peroxide),
                UCorrection(fp, MITRelaxSet, compat_type),
            ]
        )


class MITAqueousCompatibility(Compatibility):
    """
    This class implements the GGA/GGA+U mixing scheme, which allows mixing of
    entries. Note that this should only be used for VASP calculations using the
    MIT parameters (see pymatgen.io.vaspio_set MITVaspInputSet). Using
    this compatibility scheme on runs with different parameters is not valid.

    Args:
        compat_type: Two options, GGA or Advanced.  GGA means all GGA+U
            entries are excluded.  Advanced means mixing scheme is
            implemented to make entries compatible with each other,
            but entries which are supposed to be done in GGA+U will have the
            equivalent GGA entries excluded. For example, Fe oxides should
            have a U value under the Advanced scheme. A GGA Fe oxide run
            will therefore be excluded under the scheme.
        correct_peroxide: Specify whether peroxide/superoxide/ozonide
            corrections are to be applied or not.
        check_potcar_hash (bool): Use potcar hash to verify potcars are correct.
    """

    def __init__(
        self, compat_type="Advanced", correct_peroxide=True, check_potcar_hash=False
    ):
        self.compat_type = compat_type
        self.correct_peroxide = correct_peroxide
        self.check_potcar_hash = check_potcar_hash
        fp = os.path.join(MODULE_DIR, "MITCompatibility.yaml")
        super().__init__(
            [
                PotcarCorrection(MITRelaxSet, check_hash=check_potcar_hash),
                MITGasCorrection(fp),
                AnionCorrection(fp, correct_peroxide=correct_peroxide),
                UCorrection(fp, MITRelaxSet, compat_type),
                AqueousCorrection(fp),
            ]
        )


class MaterialsProjectAqueousCompatibility(Compatibility):
    """
    This class implements the GGA/GGA+U mixing scheme, which allows mixing of
    entries. Note that this should only be used for VASP calculations using the
    MaterialsProject parameters (see pymatgen.io.vaspio_set.MPVaspInputSet).
    Using this compatibility scheme on runs with different parameters is not
    valid.

    Args:
        compat_type: Two options, GGA or Advanced.  GGA means all GGA+U
            entries are excluded.  Advanced means mixing scheme is
            implemented to make entries compatible with each other,
            but entries which are supposed to be done in GGA+U will have the
            equivalent GGA entries excluded. For example, Fe oxides should
            have a U value under the Advanced scheme. A GGA Fe oxide run
            will therefore be excluded under the scheme.
        correct_peroxide: Specify whether peroxide/superoxide/ozonide
            corrections are to be applied or not.
        check_potcar_hash (bool): Use potcar hash to verify potcars are correct.
    """

    def __init__(
        self, compat_type="Advanced", correct_peroxide=True, check_potcar_hash=False
    ):
        self.compat_type = compat_type
        self.correct_peroxide = correct_peroxide
        self.check_potcar_hash = check_potcar_hash
        fp = os.path.join(MODULE_DIR, "MPCompatibility.yaml")
        super().__init__(
            [
                PotcarCorrection(MPRelaxSet, check_hash=check_potcar_hash),
                AnionCorrection(fp, correct_peroxide=correct_peroxide),
                UCorrection(fp, MPRelaxSet, compat_type),
                AqueousCorrection(fp),
            ]
        )
