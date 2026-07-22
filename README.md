# Mine-imator Suite + Import for Blender 0.3.1

Imports the frame-zero static state of Mine-imator 2.0.2 format-34 projects
into Blender 5.2. The add-on creates a new uniquely named collection on every
import, keeps Mine-imator model parts as editable pivot/mesh hierarchies, and
never creates F-curves or other animation data.

## Install and use

1. In Blender, choose **Edit > Preferences > Get Extensions > Install from Disk**.
2. Select `mineimator_mcprep_bridge-0.3.1.zip` and enable the extension.
3. Open **File > Import > Mine-imator Project**, or the **MI Bridge** tab in the
   3D View sidebar.
4. Select a `.miproject` file (or paste a directory containing exactly one),
   choose categories, run **Preflight**, then **Import Scene**.
5. Read the full result in the Text Editor's `Mine-imator Bridge Report` text.

Project-local files take priority over the matching Mine-imator Minecraft
asset ZIP. MCprep material preparation is invoked when its operator is
available, while image textures retain nearest-neighbor filtering and alpha.

Mineways is optional for the rest of the import. If configured or found in a
common portable/install location, the bridge
uses Mineways' headless scripting interface with the saved world path and the
corrected X/Y/Z crop, imports the resulting OBJ, and includes it in MCprep
material preparation. If Mineways is unavailable, world scenery becomes a
magenta placeholder containing exact setup and crop information.

Particle spawners become labeled placeholders. Audio and all keyframes after
frame zero are intentionally ignored.

## Render Export

The integrated still-image exporter is available from **File > Export Image...**
and from **3D View > Sidebar > MI Bridge > Render Export**. Choose a size preset
or custom resolution, select a camera, optionally remove the background or
include render-hidden objects, then choose **Render and Save PNG**. It renders
the current frame and restores the scene's original render settings afterward.

## 0.3.0 Render Export integration

- Integrates Simple Render Export 1.0.0 into the MI Bridge package.
- Adds nine image-size presets plus custom dimensions, camera selection,
  transparent-background PNG output, and optional hidden-object rendering.
- Adds matching File-menu and MI Bridge sidebar controls without requiring a
  second add-on installation.

## 0.3.1 Project name

- Adopts the public name **Mine-imator Suite + Import for Blender** while
  retaining the stable extension ID for seamless upgrades.

The **Use frame-0 item swaps** option is disabled by default. Mine-imator can
leave stale `ITEM_NAME` compatibility hints in a saved project (the TEST
fixture labels all five visibly different items as a leather helmet). Enable
the option only when the frame-0 swap is known to be intentional.

## 0.2.0 Mine-imator Suite

- Adds a default-enabled full environment import with Minecraft grass ground,
  day/night sky, sunlight, textured sun and moon, moon phases, pixel stars,
  Minecraft clouds, biome colors, and fog.
- Adds a live `MI Environment` sidebar modeled after Mine-imator's environment
  panels. Appearance settings update Blender immediately while wind and motion
  speeds remain stored static values.
- Repeated suite imports preserve older environments but make the newest suite
  active. Disabling Mine-imator Suite retains the legacy environment importer.

## 0.1.5 fixes

- Detect and remove Blender's untouched default startup cube during import.
  The check requires its original name, mesh, transform, collection, Camera,
  and Light, so user-created or edited cubes are preserved.
- Added an enabled-by-default option to retain the startup cube if desired.

## 0.1.4 fixes

- Hide Mine-imator's unused empty-world slot from the viewport and renders,
  while retaining it as a metadata-only Outliner entry for source accounting.

## 0.1.3 fixes

- Rebuilt pixel items using Mine-imator's bottom-up texture coordinates,
  fixed one-MI-unit extrusion depth, and saved rotation point.
- Removed internal per-pixel cube faces and generate only the visible front,
  back, and silhouette boundary surfaces.
- Corrected the apparent sword, axe, bow, and arrow angles by using
  Mine-imator's bottom-centered item pivot rather than a centered mesh origin.

## 0.1.2 fixes

- Per-instance frame-0 skin and model texture overrides.
- Correct mixed-axis scene rotation order and declared bend-angle limits.
- Correct local positions and rotations for multi-shape custom `.mimodel`
  files, including the five-piece Server Obliterator fixture.
- Template item identity by default, with opt-in frame-0 item swaps.
- Empty Mine-imator scenery slots now remain non-rendering viewport markers
  instead of producing an unexplained placeholder cube.

## 0.1.1 fixes

- Exact mixed-axis Mine-imator rotation composition for frame-zero poses.
- Correct Mine-imator cuboid UV face layout and mirrored textures.
- Blocky bend-plane splitting, eliminating stretched cuboid faces.
- Relative hierarchy linking for held items and nested timelines.
- Automatic Mineways detection plus Mine-imator/Minecraft world-axis
  correction for scenery placement.

## Development and verification

Run the pure parser tests from the add-on folder:

```powershell
python tests\test_core.py
```

Run the Blender integration fixture from the add-on folder:

```powershell
& 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe' --background --factory-startup --python tests\blender_test_project.py -- --project 'C:\path\to\TEST.miproject'
```
