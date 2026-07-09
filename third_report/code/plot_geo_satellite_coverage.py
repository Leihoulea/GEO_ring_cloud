import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import cartopy.crs as ccrs
import numpy as np


def format_lon(lon):
    hemisphere = "E" if lon >= 0 else "W"
    return f"{abs(lon):.1f} deg {hemisphere}"


def normalize_longitudes(lons):
    return ((lons + 180.0) % 360.0) - 180.0


def geostationary_limb_lonlat(subsatellite_lon, satellite_height, n_points=1441):
    """Return the spherical Earth horizon curve seen from geostationary orbit."""
    earth_equatorial_radius = 6_378_137.0
    horizon_angle = np.arccos(
        earth_equatorial_radius / (earth_equatorial_radius + satellite_height)
    )
    azimuth = np.linspace(0.0, 2.0 * np.pi, n_points)

    lat = np.degrees(np.arcsin(np.sin(horizon_angle) * np.cos(azimuth)))
    lon_offset = np.degrees(
        np.arctan2(np.sin(azimuth) * np.sin(horizon_angle), np.cos(horizon_angle))
    )
    lon = normalize_longitudes(subsatellite_lon + lon_offset)
    return lon, lat


def plot_wrapped_curve(ax, lon, lat, **kwargs):
    jumps = np.where(np.abs(np.diff(lon)) > 180.0)[0] + 1
    for lon_part, lat_part in zip(np.split(lon, jumps), np.split(lat, jumps)):
        if len(lon_part) > 1:
            ax.plot(lon_part, lat_part, transform=ccrs.PlateCarree(), **kwargs)


def plot_precise_geo_coverage(output_path="accurate_geo_coverage.png"):
    fig = plt.figure(figsize=(18, 9), facecolor="black")
    ax = plt.axes(projection=ccrs.PlateCarree())
    ax.set_global()
    ax.set_facecolor("black")

    ax.stock_img()
    ax.coastlines(color="white", linewidth=0.5, alpha=0.85)

    gl = ax.gridlines(
        draw_labels=True,
        dms=True,
        x_inline=False,
        y_inline=False,
        color="gray",
        linestyle="--",
        linewidth=0.7,
        alpha=0.55,
    )
    gl.top_labels = False
    gl.right_labels = False
    gl.xlabel_style = {"color": "white", "size": 8}
    gl.ylabel_style = {"color": "white", "size": 8}

    satellites = [
        {"name": "GOES-West", "lon": -137.2, "color": "#FF8C00"},
        {"name": "GOES-East", "lon": -75.2, "color": "#FFD700"},
        {"name": "MSG", "lon": 0.0, "color": "#D3D3D3"},
        {"name": "IODC", "lon": 41.5, "color": "#DA70D6"},
        {"name": "FY-4B", "lon": 105.0, "color": "#32CD32"},
        {"name": "Himawari", "lon": 140.7, "color": "#00BFFF"},
    ]

    satellite_height = 35_786_000.0

    for sat in satellites:
        lon = sat["lon"]
        color = sat["color"]
        name = sat["name"]

        ax.plot(
            lon,
            0,
            marker="o",
            color=color,
            markeredgecolor="black",
            markeredgewidth=0.8,
            markersize=10,
            transform=ccrs.PlateCarree(),
            zorder=5,
        )
        ax.text(
            lon,
            -6,
            f"{name}\n{format_lon(lon)}",
            color=color,
            ha="center",
            va="top",
            weight="bold",
            fontsize=10,
            transform=ccrs.PlateCarree(),
            zorder=6,
        )

        limb_lon, limb_lat = geostationary_limb_lonlat(lon, satellite_height)
        plot_wrapped_curve(
            ax,
            limb_lon,
            limb_lat,
            color=color,
            linestyle="--",
            linewidth=2.3,
            alpha=0.95,
            zorder=4,
        )

    earth_equatorial_radius = 6_378_137.0
    limit_lat = np.degrees(
        np.arccos(earth_equatorial_radius / (earth_equatorial_radius + satellite_height))
    )

    ax.axhline(limit_lat, color="white", linestyle=":", linewidth=1.4, alpha=0.8)
    ax.axhline(-limit_lat, color="white", linestyle=":", linewidth=1.4, alpha=0.8)
    ax.text(
        -176,
        limit_lat + 2,
        f"+/-{limit_lat:.1f} deg theoretical horizon latitude",
        color="white",
        fontsize=9,
        transform=ccrs.PlateCarree(),
    )

    ax.set_title(
        "Geostationary Meteorological Satellites: Nominal Earth-Disc Coverage",
        fontsize=16,
        color="white",
        pad=20,
    )

    plt.tight_layout()
    plt.savefig(output_path, dpi=300, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    return output_path


if __name__ == "__main__":
    print(plot_precise_geo_coverage())
