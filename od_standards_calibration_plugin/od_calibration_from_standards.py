# -*- coding: utf-8 -*-
"""
https://docs.pioreactor.com/developer-guide/adding-calibration-type
"""
from __future__ import annotations

from time import sleep
from typing import Callable
from typing import cast
from typing import Type

import click
from click import echo, style
from msgspec.json import decode
from msgspec.json import encode

from pioreactor import structs
from pioreactor import types as pt
from pioreactor.background_jobs.stirring import start_stirring as stirring
from pioreactor.background_jobs.stirring import Stirrer
from pioreactor.config import config
from pioreactor.config import leader_address
from pioreactor.mureq import patch
from pioreactor.mureq import put
from pioreactor.pubsub import put_into_leader, patch_into_leader
from pioreactor.mureq import HTTPErrorStatus
from pioreactor.utils import is_pio_job_running
from pioreactor.utils import local_persistant_storage
from pioreactor.utils import managed_lifecycle
from pioreactor.utils.timing import current_utc_datetime
from pioreactor.whoami import get_testing_experiment_name
from pioreactor.whoami import get_unit_name
from pioreactor.whoami import is_testing_env

def green(string: str) -> str:
    return style(string, fg="green")


def red(string: str) -> str:
    return style(string, fg="red")


def bold(string: str) -> str:
    return style(string, bold=True)



def introduction() -> None:
    import logging

    logging.disable(logging.WARNING)

    click.clear()
    click.echo(
        """This routine will calibrate the current Pioreactor to (offline) OD600 readings using a set of standards. You'll need:
    1. A Pioreactor
    2. A set of OD600 standards in Pioreactor vials (at least 10 mL in each vial)
"""
    )


def get_metadata_from_user():
    from math import log2

    with local_persistant_storage("od_calibrations") as cache:
        while True:
            name = click.prompt("Provide a name for this calibration", type=str).strip()
            if name == "":
                click.echo("Name cannot be empty")
                continue
            elif name in cache:
                if click.confirm("❗️ Name already exists. Do you wish to overwrite?"):
                    break
            elif name == "current":
                click.echo("Name cannot be `current`.")
                continue
            else:
                break

    if "REF" not in config["od_config.photodiode_channel_reverse"]:
        raise ValueError("REF required for OD calibration.")
        # technically it's not required? we just need a specific PD channel to calibrate from.

    ref_channel = config["od_config.photodiode_channel_reverse"]["REF"]
    signal_channel = "1" if ref_channel == "2" else "2"

    click.confirm(
        f"Confirm using channel {signal_channel} with angle {config['od_config.photodiode_channel'][signal_channel]}° position in the Pioreactor",
        abort=True,
        default=True,
    )
    angle = str(config["od_config.photodiode_channel"][signal_channel])
    return name, angle, signal_channel


def setup_HDC_instructions() -> None:
    click.clear()
    click.echo(
        """ Setting up:
    1. Place first standard into Pioreactor, with a stir bar.
"""
    )


def choose_settings() -> float:
    config_rpm = config.getfloat("stirring", "target_rpm")

    rpm = click.prompt(
        click.style(
            f"Optional: Enter RPM for stirring. [enter] for {config_rpm} RPM, default set in config.ini",
            fg="green",
        ),
        type=click.FloatRange(0, 10000),
        default=config_rpm,
        show_default=False,
    )
    return rpm


def start_stirring(target_rpm: int) -> Stirrer:
    while not click.confirm("Reading to start stirring?", default=True):
        pass

    if target_rpm is None:
        target_rpm = config.getfloat("stirring", "target_rpm")

    click.echo("Starting stirring.")

    st = stirring(
        target_rpm=target_rpm,
        unit=get_unit_name(),
        experiment=get_testing_experiment_name(),
    )
    st.block_until_rpm_is_close_to_target(abs_tolerance=120)
    return st

def plot_data(
    x,
    y,
    title,
    x_min=None,
    x_max=None,
    interpolation_curve=None,
    highlight_recent_point=True,
):
    import plotext as plt  # type: ignore

    plt.clf()

    plt.scatter(x, y, marker="hd")

    if highlight_recent_point:
        plt.scatter([x[-1]], [y[-1]], color=204, marker="hd")

    plt.theme("pro")
    plt.title(title)
    plt.xlabel("OD600")
    plt.ylabel("OD Reading (Raw)")

    plt.plot_size(105, 22)

    if interpolation_curve:
        plt.plot(sorted(x), [interpolation_curve(x_) for x_ in sorted(x)], color=204)
        plt.plot_size(145, 26)

    plt.xlim(x_min, x_max)
    plt.yfrequency(6)
    plt.xfrequency(6)

    plt.show()


def start_recording_standards(st: Stirrer, signal_channel):
    
    from pioreactor.background_jobs.od_reading import start_od_reading
    voltages = []
    od600_values = []
    click.echo("Starting OD recordings.")

    with start_od_reading(
        cast(pt.PdAngleOrREF, config.get("od_config.photodiode_channel", "1")),
        cast(pt.PdAngleOrREF, config.get("od_config.photodiode_channel", "2")),
        interval=None,
        unit=get_unit_name(),
        fake_data=is_testing_env(),
        experiment=get_testing_experiment_name(),
        use_calibration=False,
    ) as od_reader:

        def get_voltage_from_adc() -> float:
            od_readings1 = od_reader.record_from_adc()
            od_readings2 = od_reader.record_from_adc()
            return 0.5 * (
                od_readings1.ods[signal_channel].od
                + od_readings2.ods[signal_channel].od
            )

        for _ in range(4):
            # warm up
            od_reader.record_from_adc()

    while True:
        click.echo("Recording next standard.")
        standard_od = click.prompt("Enter OD600 measurement", type=float)
        for i in range(4):
            click.echo(".", nl=False)
            sleep(0.5)

        click.echo(".", nl=False)
        voltage = get_voltage_from_adc()
        click.echo(".", nl=False)

        od600_values.append(standard_od)
        voltages.append(voltage)

        st.set_state("sleeping")

        for i in range(len(od600_values)):
            click.clear()
            plot_data(
                od600_values,
                voltages,
                title="OD Calibration (ongoing)",
                x_min=0,
                x_max=max(od600_values),
            )
            click.echo()

        if not click.confirm("Record another OD600 standard?", default=True):
            break

        click.echo()
        click.echo(click.style("Stop❗", fg="red"))
        click.echo("Carefully remove vial and replace with next standard.")
        click.echo("Confirm vial outside is dry and clean.")
        while not click.confirm("Continue?", default=True):
            pass
        st.set_state("ready")
        st.block_until_rpm_is_close_to_target(abs_tolerance=120)
        sleep(1.0)

    click.clear()
    plot_data(
        od600_values,
        voltages,
        title="OD Calibration (ongoing)",
        x_min=0,
        x_max=max(od600_values),
    )
    click.echo("Add media blank standard.")
    od600_blank = click.prompt("What is the OD600 of your blank?", type=float)
    click.echo("Confirm vial outside is dry and clean. Place into Pioreactor.")
    while not click.confirm("Continue?", default=True):
        pass

    voltages.append(get_voltage_from_adc())
    od600_values.append(od600_blank)

    return od600_values, voltages


def calculate_curve_of_best_fit(
    voltages: list[float], od600_values: list[float], degree: int
) -> tuple[list[float], str]:
    import numpy as np

    # weigh the last point, the "blank measurement", more.
    # 1. It's far away from the other points
    # 2. We have prior knowledge that OD~0 when V~0.
    n = len(voltages)
    weights = np.ones_like(voltages)
    weights[-1] = n / 2

    try:
        coefs = np.polyfit(od600_values, voltages, deg=degree, w=weights).tolist()
    except Exception:
        click.echo("Unable to fit.")
        coefs = np.zeros(degree).tolist()

    return coefs, "poly"


def show_results_and_confirm_with_user(
    curve_data: list[float],
    curve_type: str,
    voltages: list[float],
    od600_values: list[float],
) -> tuple[bool, int]:
    click.clear()

    curve_callable = curve_to_callable(curve_type, curve_data)

    plot_data(
        od600_values,
        voltages,
        title="OD Calibration with curve of best fit",
        interpolation_curve=curve_callable,
        highlight_recent_point=False,
    )
    click.echo()
    click.echo(f"Calibration curve: {curve_to_functional_form(curve_type, curve_data)}")
    r = click.prompt(
        """
What next?

Y: confirm and save to disk
n: abort completely
d: choose a new degree for polynomial fit

""",
        type=click.Choice(["Y", "n", "d"]),
    )
    if r == "Y":
        return True, -1
    elif r == "n":
        raise click.Abort()
    elif r == "d":
        d = click.prompt("Enter new degree", type=click.IntRange(1, 5, clamp=True))
        return False, d
    else:
        raise click.Abort()


def save_results(
    curve_data_: list[float],
    curve_type: str,
    voltages: list[float],
    od600_values: list[float],
    angle,
    name: str,
    signal_channel: pt.PdChannel,
    unit: str,
) -> structs.ODCalibration:
    if angle == "45":
        struct: Type[structs.ODCalibration] = structs.OD45Calibration
    elif angle == "90":
        struct = structs.OD90Calibration
    elif angle == "135":
        struct = structs.OD135Calibration
    elif angle == "180":
        struct = structs.OD180Calibration
    else:
        raise ValueError()

    data_blob = struct(
        created_at=current_utc_datetime(),
        pioreactor_unit=unit,
        name=name,
        angle=angle,
        maximum_od600=max(od600_values),
        minimum_od600=0,
        minimum_voltage=min(voltages),
        maximum_voltage=max(voltages),
        curve_data_=curve_data_,
        curve_type=curve_type,
        voltages=voltages,
        od600s=od600_values,
        ir_led_intensity=float(config["od_reading.config"]["ir_led_intensity"]),
        pd_channel=signal_channel,
    )

    with local_persistant_storage("od_calibrations") as cache:
        cache[name] = encode(data_blob)

    publish_to_leader(name)
    change_current(name)

    return data_blob


def od_calibration_from_standards() -> None:
    unit = get_unit_name()
    experiment = get_testing_experiment_name()

    if any(is_pio_job_running(["stirring", "od_reading"])):
        raise ValueError("Stirring and OD reading should be turned off.")

    with managed_lifecycle(
        unit, experiment, "od_calibration_from_standards"
    ):
        introduction()
        (
            name,
            angle,
            signal_channel,
        ) = get_metadata_from_user()
        setup_HDC_instructions()

        is_ready = True
        while is_ready:
            target_rpm = choose_settings()

            is_ready = click.confirm(
                click.style("Do you want to change the stirring RPM?", fg="green"),
                prompt_suffix=" ",
                default=False,
            )

        with start_stirring(target_rpm) as st:
            od600_values, voltages = start_recording_standards(st, signal_channel)

        degree = 4
        while True:
            curve_data_, curve_type = calculate_curve_of_best_fit(
                voltages, od600_values, degree
            )
            okay_with_result, degree = show_results_and_confirm_with_user(
                curve_data_, curve_type, voltages, od600_values
            )
            if okay_with_result:
                break

        data_blob = save_results(
            curve_data_,
            curve_type,
            voltages,
            od600_values,
            angle,
            name,
            signal_channel,
            unit,
        )
        click.echo(click.style(f"Data for {name}", underline=True, bold=True))
        click.echo(data_blob)
        click.echo()
        click.echo(
            click.style(f"Calibration curve for `{name}`", underline=True, bold=True)
        )
        click.echo(curve_to_functional_form(curve_type, curve_data_))
        click.echo()
        click.echo(f"Finished calibration of {name} ✅")

        if not config.getboolean("od_reading.config", "use_calibration", fallback=False):
            click.echo()
            click.echo(
                click.style(
                    "Currently [od_reading.config][use_calibration] is set to 0 in your config.ini. This should be set to 1 to use calibrations.",
                    bold=True,
                )
            )
        return


def curve_to_functional_form(curve_type: str, curve_data) -> str:
    if curve_type == "poly":
        d = len(curve_data)
        return " + ".join(
            [
                (f"{c:0.3f}x^{d - i - 1}" if (i < d - 1) else f"{c:0.3f}")
                for i, c in enumerate(curve_data)
            ]
        )
    else:
        raise ValueError()


def curve_to_callable(curve_type: str, curve_data) -> Callable:
    if curve_type == "poly":
        import numpy as np

        def curve_callable(x):
            return np.polyval(curve_data, x)

        return curve_callable

    else:
        raise NotImplementedError


def display(name: str | None) -> None:
    from pprint import pprint

    def display_from_calibration_blob(data_blob) -> None:
        voltages = data_blob["voltages"]
        ods = data_blob["od600s"]
        name, angle = data_blob["name"], data_blob["angle"]
        click.echo()
        click.echo(click.style(f"Calibration `{name}`", underline=True, bold=True))
        plot_data(
            ods,
            voltages,
            title=f"`{name}`, calibration of {angle}°",
            highlight_recent_point=False,
            interpolation_curve=curve_to_callable(
                data_blob["curve_type"], data_blob["curve_data_"]
            ),
        )
        click.echo()
        click.echo(
            click.style(f"Calibration curve for `{name}`", underline=True, bold=True)
        )
        click.echo(
            curve_to_functional_form(data_blob["curve_type"], data_blob["curve_data_"])
        )
        click.echo()
        click.echo(click.style(f"Data for `{name}`", underline=True, bold=True))
        pprint(data_blob)

    if name is not None:
        with local_persistant_storage("od_calibrations") as c:
            display_from_calibration_blob(decode(c[name]))
    else:
        with local_persistant_storage("current_od_calibration") as c:
            for angle in c.iterkeys():
                display_from_calibration_blob(decode(c[angle]))
                click.echo()
                click.echo()
                click.echo()


def publish_to_leader(name: str) -> bool:
    success = True

    with local_persistant_storage("od_calibrations") as all_calibrations:
        calibration_result = decode(
            all_calibrations[name], type=structs.subclass_union(structs.ODCalibration)
        )

    try:
        res = put_into_leader("/api/calibrations", json=calibration_result)
        res.raise_for_status()
        echo("✅ Published to leader.")
    except Exception as e:
        success = False
        print(e)
        echo(f"Could not update in database on leader at http://{leader_address}/api/calibrations ❌")

    return success


def change_current(name: str) -> None:
    try:
        with local_persistant_storage("od_calibrations") as all_calibrations:
            new_calibration = decode(
                all_calibrations[name],
                type=structs.subclass_union(structs.ODCalibration),
            )

        angle = new_calibration.angle
        with local_persistant_storage("current_od_calibration") as current_calibrations:
            if angle in current_calibrations:
                old_calibration = decode(
                    current_calibrations[angle],
                    type=structs.subclass_union(structs.ODCalibration),
                )
            else:
                old_calibration = None

            current_calibrations[angle] = encode(new_calibration)

        try:
            res = patch_into_leader(
                f"/api/calibrations/{get_unit_name()}/{new_calibration.type}/{new_calibration.name}",
                json={"current": 1},
            )
            res.raise_for_status()
        except HTTPErrorStatus as e:
            if e.status_code == 404:
                # it doesn't exist in leader, so lets put it there.
                publish_to_leader(name)
                change_current(name)
            else:
                echo("Could not update in database on leader ❌")
        else:
            if old_calibration:
                echo(f"Replaced `{old_calibration.name}` with `{new_calibration.name}`   ✅")
            else:
                echo(f"Set `{new_calibration.name}` to current calibration  ✅")

    except Exception as e:
        echo(red(f"Failed to swap. {e}"))
        raise click.Abort()


def list_() -> None:
    # get current calibrations
    current = []
    with local_persistant_storage("current_od_calibration") as c:
        for _ in c.iterkeys():
            cal = decode(c[_], type=structs.subclass_union(structs.ODCalibration))
            current.append(cal.name)

    click.secho(
        f"{'Name':15s} {'Date':18s} {'Angle':12s} {'Currently in use?':20s}",
        bold=True,
    )
    with local_persistant_storage("od_calibrations") as c:
        for name in c.iterkeys():
            try:
                cal = decode(
                    c[name], type=structs.subclass_union(structs.ODCalibration)
                )
                click.secho(
                    f"{cal.name:15s} {cal.created_at:%d %b, %Y}       {cal.angle:12s} {'✅' if cal.name in current else ''}",
                )
            except Exception:
                pass


@click.group(invoke_without_command=True, name="od_calibration_from_standards")
@click.pass_context
def click_od_calibration_from_standards(ctx):
    """
    Calibrate OD600 to voltages
    """
    if ctx.invoked_subcommand is None:
        od_calibration_from_standards()


@click_od_calibration_from_standards.command(name="display")
@click.option("-n", "--name", type=click.STRING)
def click_display(name: str):
    display(name)


@click_od_calibration_from_standards.command(name="change_current")
@click.argument("name", type=click.STRING)
def click_change_current(name: str):
    change_current(name)


@click_od_calibration_from_standards.command(name="list")
def click_list():
    list_()


@click_od_calibration_from_standards.command(name="publish")
@click.argument("name", type=click.STRING)
def click_publish(name: str):
    publish_to_leader(name)
