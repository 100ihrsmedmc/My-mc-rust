//! Headless probe of Horizon's Iris/OptiFine shader-pack path.
//!
//! For every pack it is given it reports, with no GPU involved, whether the pack
//! can be discovered, opened, preprocessed, translated to naga modules and lowered
//! to the shader language the iOS build uses (Metal/MSL) -- and, for comparison,
//! to the SPIR-V, HLSL and GLSL writers wgpu also uses. This is the same code the
//! game runs (`horizon-shaders`), so a pack that fails here is a pack the game
//! cannot apply, and a pack that passes is one the loader can hand to the renderer.
//!
//! It says nothing about pixels: MSL that compiles is not proof that a pack renders
//! as its author intended. That is what the report's caveats are for.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use horizon_shaders::{
    discover, dimension_folder, load_post, Pack, Properties, Macros, Lang,
    GbuffersInputs, GameInputs, GeometryInputs, Parts, Post,
};

/// The vertex inputs the game supplies to a pack's programs. Mirrors
/// `crates/horizon-shaders/tests/it/real_packs.rs`, which mirrors the renderer.
fn terrain_inputs() -> GbuffersInputs {
    GbuffersInputs {
        declarations: "layout(location = 0) in vec3 hz_v_pos;".into(),
        body: "    hz_Vertex = vec4(hz_v_pos, 1.0);\n    hz_Color = vec4(1.0);\n".into(),
        attributes: vec![("mc_Entity".into(), "vec4(-1.0)".into())],
        ..Default::default()
    }
}

fn game_inputs(terrain: &GbuffersInputs) -> GameInputs {
    GameInputs {
        terrain: Some(terrain.clone()),
        sky: Some(terrain.clone()),
        celestial: Some(terrain.clone()),
        entities: Some(terrain.clone()),
        clouds: Some(terrain.clone()),
        weather: Some(terrain.clone()),
        lines: Some(terrain.clone()),
        hand: Some(GbuffersInputs {
            item_id_declared: true,
            ..terrain.clone()
        }),
        terrain_parts: Some([terrain.clone(), terrain.clone()]),
        terrain_one_pass: Some(GbuffersInputs {
            parts: Some(Parts {
                solid: "hz_v_pos.x > 0.0".into(),
                cutout_alpha_test: None,
            }),
            ..terrain.clone()
        }),
        terrain_geometry: Some(GeometryInputs {
            inputs: GbuffersInputs {
                declarations: format!(
                    "layout(set = 3, binding = 1) readonly buffer HzVertices {{ uint hz_vertices[]; }};\n{}",
                    terrain.declarations
                ),
                ..terrain.clone()
            },
            pull: "        hz_v_pos = uintBitsToFloat(uvec3(hz_vertices[hz_vertex_index * 6u], \
                   hz_vertices[hz_vertex_index * 6u + 1u], hz_vertices[hz_vertex_index * 6u + 2u]));\n"
                .into(),
            index: "uint(@)".into(),
        }),
        entity_geometry: Some(GeometryInputs {
            inputs: GbuffersInputs {
                declarations: format!(
                    "layout(set = 1, binding = 0) readonly buffer HzEntityVertices {{ uint hz_entity_vertices[]; }};\n{}",
                    terrain.declarations
                ),
                ..terrain.clone()
            },
            pull: "        hz_v_pos = uintBitsToFloat(uvec3(hz_entity_vertices[hz_vertex_index * 11u], \
                   hz_entity_vertices[hz_vertex_index * 11u + 1u], hz_entity_vertices[hz_vertex_index * 11u + 2u]));\n"
                .into(),
            index: "uint(@)".into(),
        }),
    }
}

/// Lower every module of a program set through a writer, counting failures.
fn lower(name: &str, module: &naga::Module, backend: Backend, failures: &mut Vec<String>) {
    let info = match naga::valid::Validator::new(
        naga::valid::ValidationFlags::all(),
        naga::valid::Capabilities::all(),
    )
    .validate(module)
    {
        Ok(info) => info,
        Err(error) => {
            failures.push(format!("{name}: {backend:?}: does not validate: {error}"));
            return;
        }
    };
    let Some(entry) = module.entry_points.first() else {
        return;
    };
    let stage = entry.stage;
    let point = entry.name.clone();
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| -> Result<(), String> {
        match backend {
            Backend::Msl => {
                use naga::back::msl;
                let options = msl::Options {
                    lang_version: (2, 4),
                    fake_missing_bindings: true,
                    ..Default::default()
                };
                msl::write_string(module, &info, &options, &msl::PipelineOptions::default())
                    .map(drop)
                    .map_err(|e| e.to_string())
            }
            Backend::Spv => {
                use naga::back::spv;
                let options = spv::Options {
                    lang_version: (1, 3),
                    flags: spv::WriterFlags::LABEL_VARYINGS | spv::WriterFlags::FORCE_POINT_SIZE,
                    ..Default::default()
                };
                let pipeline = spv::PipelineOptions {
                    shader_stage: stage,
                    entry_point: point.clone(),
                };
                spv::write_vec(module, &info, &options, Some(&pipeline))
                    .map(drop)
                    .map_err(|e| e.to_string())
            }
            Backend::Hlsl => {
                use naga::back::hlsl;
                let options = hlsl::Options {
                    shader_model: hlsl::ShaderModel::V6_0,
                    fake_missing_bindings: true,
                    ..Default::default()
                };
                let pipeline = hlsl::PipelineOptions {
                    entry_point: Some((stage, point.clone())),
                };
                let mut out = String::new();
                hlsl::Writer::new(&mut out, &options, &pipeline)
                    .write(module, &info, None)
                    .map(drop)
                    .map_err(|e| e.to_string())
            }
            Backend::Glsl => {
                use naga::back::glsl;
                let options = glsl::Options {
                    version: glsl::Version::Desktop(430),
                    ..Default::default()
                };
                let pipeline = glsl::PipelineOptions {
                    shader_stage: stage,
                    entry_point: point.clone(),
                    multiview: None,
                };
                let mut out = String::new();
                glsl::Writer::new(
                    &mut out,
                    module,
                    &info,
                    &options,
                    &pipeline,
                    naga::proc::BoundsCheckPolicies::default(),
                )
                .and_then(|mut w| w.write().map(drop))
                .map_err(|e| e.to_string())
            }
        }
    }));
    match result {
        Ok(Ok(())) => {}
        Ok(Err(error)) => failures.push(format!("{name}: {backend:?}: {error}")),
        Err(_) => failures.push(format!("{name}: {backend:?}: writer panicked")),
    }
}

#[derive(Clone, Copy, Debug)]
enum Backend {
    Msl,
    Spv,
    Hlsl,
    Glsl,
}

fn all_modules(post: &Post) -> Vec<(String, &naga::Module)> {
    let optional = [
        &post.terrain,
        &post.terrain_cutout,
        &post.water,
        &post.sky,
        &post.sky_textured,
        &post.entities,
        &post.block,
        &post.entities_translucent,
        &post.particles,
        &post.clouds,
        &post.weather,
        &post.hand,
        &post.shadow,
        &post.shadow_water,
        &post.shadow_entities,
        &post.shadow_block,
    ];
    let mut modules = Vec::new();
    for program in post
        .programs
        .iter()
        .chain(&post.shadow_comp)
        .chain(optional.into_iter().flatten())
    {
        if let Some(translated) = &program.program {
            modules.push((format!("{} vertex", program.name), &translated.vertex));
            modules.push((format!("{} fragment", program.name), &translated.fragment));
        }
        for (name, compute) in &program.compute {
            modules.push((name.clone(), &compute.module));
        }
    }
    modules
}

fn probe_pack(pack: &Pack, probes: &mut Vec<String>, errors: &mut Vec<String>) -> serde_json::Value {
    let mut entry = serde_json::Map::new();
    entry.insert("name".into(), pack.name().into());

    let mut macros = Macros::standard();
    macros.define_renderer(4);

    // shaders.properties and the id files, as the game reads them.
    let (properties, notes) = horizon_shaders::read_properties(pack, &mut macros);
    let id_files: BTreeMap<String, usize> = ["block", "item", "entity", "dimension"]
        .iter()
        .filter_map(|name| {
            pack.read_text(&format!("{name}.properties"))
                .map(|text| (name.to_string(), Properties::parse(&text, &macros).entries.len()))
        })
        .collect();
    entry.insert("shaders_properties_keys".into(), properties.entries.len().into());
    entry.insert("feature_notes".into(), serde_json::json!(notes));
    entry.insert("id_files".into(), serde_json::json!(id_files));
    let lang = Lang::load(pack, "en_us");
    entry.insert("lang_read".into(), serde_json::json!(lang != Lang::default()));

    // Preprocess every stage file of the base and each dimension folder.
    let mut stage_files = 0;
    let mut diagnostics = 0;
    for dir in ["", "world0", "world-1", "world1"] {
        for file in pack.list(dir) {
            if [".vsh", ".fsh", ".gsh", ".csh"].iter().any(|e| file.ends_with(e)) {
                let path = if dir.is_empty() { file.clone() } else { format!("{dir}/{file}") };
                let preprocessed = horizon_shaders::preprocess(pack, &path, &macros);
                stage_files += 1;
                diagnostics += preprocessed.diagnostics.len();
            }
        }
    }
    entry.insert("stage_files".into(), stage_files.into());
    entry.insert("preprocess_diagnostics".into(), diagnostics.into());

    // Every dimension's program set, loaded as the game loads it, and lowered.
    let terrain = terrain_inputs();
    let inputs = game_inputs(&terrain);
    let mut programs = 0;
    let mut backend_failures: Vec<String> = Vec::new();
    let mut notes_total = 0;
    let mut dimensions = serde_json::Map::new();
    for id in [0, -1, 1] {
        let folder = dimension_folder(pack, id);
        let shaders = Properties::from_pack(pack, &folder, &macros).in_dimension(id);
        let post = load_post(pack, &folder, &shaders, &macros, &inputs);
        let modules = all_modules(&post);
        programs += modules.len();
        notes_total += post.notes.len() + post.config.notes.len();
        for (name, module) in &modules {
            for backend in [Backend::Msl, Backend::Spv, Backend::Hlsl, Backend::Glsl] {
                lower(name, module, backend, &mut backend_failures);
            }
        }
        dimensions.insert(
            format!("{id}"),
            serde_json::json!({
                "folder": folder,
                "post_programs": post.programs.len(),
                "shadow_pass": post.shadow_pass,
                "shadowcomp": post.shadow_comp.len(),
                "terrain": post.terrain.as_ref().map(|t| t.name.clone()),
                "water": post.water.as_ref().map(|t| t.name.clone()),
                "sky": post.sky.as_ref().map(|t| t.name.clone()),
                "entities": post.entities.as_ref().map(|t| t.name.clone()),
                "modules": modules.len(),
            }),
        );
    }
    entry.insert("dimensions".into(), serde_json::Value::Object(dimensions));
    entry.insert("modules_lowered".into(), programs.into());
    entry.insert("post_notes".into(), notes_total.into());
    entry.insert("backend_failures".into(), serde_json::json!(backend_failures));

    let applies = !pack.all_files().is_empty();
    entry.insert("applies".into(), serde_json::json!(applies && backend_failures.is_empty()));

    if backend_failures.is_empty() {
        probes.push(format!("{}: {} stage files, {programs} modules lower to MSL/SPV/HLSL/GLSL", pack.name(), stage_files));
    } else {
        errors.push(format!(
            "{}: {} of {} module/backend lowers failed; first: {}",
            pack.name(),
            backend_failures.len(),
            programs * 4,
            backend_failures[0]
        ));
    }
    serde_json::Value::Object(entry)
}

fn pack_paths(args: &[String]) -> Result<Vec<PathBuf>, String> {
    let mut paths = Vec::new();
    let mut index = 0;
    while index < args.len() {
        let arg = &args[index];
        if arg == "--dir" {
            index += 1;
            let dir = args.get(index).ok_or("--dir needs a folder")?;
            // Exactly what the game reads in a `shaderpacks/` folder: every direct
            // entry, one level deep, no deeper.
            for found in discover(Path::new(dir)) {
                paths.push(found.path);
            }
        } else if arg == "--report" {
            index += 1;
        } else if !arg.starts_with("--") {
            paths.push(PathBuf::from(arg));
        }
        index += 1;
    }
    Ok(paths)
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let report_path = args
        .iter()
        .position(|a| a == "--report")
        .and_then(|i| args.get(i + 1))
        .map(PathBuf::from);
    let paths = match pack_paths(&args) {
        Ok(paths) => paths,
        Err(error) => {
            eprintln!("error: {error}");
            return ExitCode::from(2);
        }
    };
    if paths.is_empty() {
        eprintln!("usage: horizon-shader-probe [--dir FOLDER | PACK...] [--report PATH]");
        return ExitCode::from(2);
    }

    let mut probes = Vec::new();
    let mut errors = Vec::new();
    let mut packs = Vec::new();
    for path in &paths {
        match Pack::open(path) {
            Ok(pack) => packs.push(probe_pack(&pack, &mut probes, &mut errors)),
            Err(error) => {
                errors.push(format!("{}: cannot open: {error}", path.display()));
                packs.push(serde_json::json!({
                    "path": path.display().to_string(),
                    "opened": false,
                    "error": error.to_string(),
                }));
            }
        }
    }

    let report = serde_json::json!({
        "packs": packs,
        "lowered": probes,
        "failures": errors,
        "caveat": "MSL/SPIR-V/HLSL/GLSL lowering proves the loader can hand a translated \
                   module to the renderer's shader writer. It does not prove the pack renders \
                   as intended on a device: that needs a GPU (a physical iPad), which this \
                   headless probe does not have.",
        "ok": errors.is_empty(),
    });
    if let Some(path) = &report_path {
        if let Err(error) = std::fs::write(path, serde_json::to_string_pretty(&report).unwrap()) {
            eprintln!("error: writing {}: {error}", path.display());
        }
    }
    for line in &probes {
        println!("  {line}");
    }
    for line in &errors {
        println!("  FAIL {line}");
    }
    println!(
        "{} pack(s): {} lowered cleanly, {} failed",
        paths.len(),
        probes.len(),
        errors.len()
    );
    if errors.is_empty() {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}
