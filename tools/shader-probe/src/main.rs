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
    discover, dimension_folder, load_post, options, Pack, Properties, Macros, Lang, Screens,
    GbuffersInputs, GameInputs, GeometryInputs, Parts, Post,
};

/// The renderer's own next step after lowering: hand the naga module to wgpu.
///
/// `naga`'s writers only prove a module *can* be written in a target language.
/// The game does one more thing the writers do not: `pack_post.rs` calls
/// `wgpu::Device::create_shader_module(ShaderSource::Naga(..))`, and wgpu's own
/// validation (and, on a real backend, the driver's compiler) is what decides
/// whether the module is accepted. This stage runs that call headless against a
/// software adapter when one is present, so the probe covers the renderer's real
/// boundary instead of stopping one step short of it.
#[cfg(feature = "gpu")]
mod gpu {
    /// A headless wgpu device, or the reason there is none (no adapter).
    pub struct Gpu {
        device: wgpu::Device,
        pub adapter: String,
        pub backend: String,
    }

    impl Gpu {
        /// Opens the first adapter any backend offers, without a display handle.
        ///
        /// `required` names a backend that must be the one selected (`metal`,
        /// `vulkan`, ...). It exists so a run on a machine with a real GPU can
        /// prove which backend actually accepted the modules, rather than
        /// reporting whichever adapter happened to come first.
        pub fn open(required: Option<&str>) -> Result<Self, String> {
            let instance = wgpu::Instance::new(wgpu::InstanceDescriptor {
                backends: match required {
                    Some("metal") => wgpu::Backends::METAL,
                    Some("vulkan") => wgpu::Backends::VULKAN,
                    Some("dx12") => wgpu::Backends::DX12,
                    Some("gl") => wgpu::Backends::GL,
                    Some(other) => return Err(format!("unknown backend {other:?}")),
                    None => wgpu::Backends::all(),
                },
                ..wgpu::InstanceDescriptor::new_without_display_handle()
            });
            let adapter = pollster::block_on(instance.request_adapter(&wgpu::RequestAdapterOptions {
                power_preference: wgpu::PowerPreference::LowPower,
                ..Default::default()
            }))
            .map_err(|e| format!("no wgpu adapter: {e}"))?;
            let info = adapter.get_info();
            let backend = format!("{:?}", info.backend);
            if let Some(want) = required {
                if !backend.eq_ignore_ascii_case(want) {
                    return Err(format!("the adapter is on {backend}, not the required {want}"));
                }
            }
            let (device, _queue) = pollster::block_on(adapter.request_device(&wgpu::DeviceDescriptor {
                label: Some("horizon-shader-probe"),
                ..Default::default()
            }))
            .map_err(|e| format!("no wgpu device: {e}"))?;
            Ok(Self {
                device,
                adapter: format!("{} ({})", info.name, backend),
                backend,
            })
        }

        /// Runs the renderer's `create_shader_module` call on one module.
        ///
        /// Returns the validation error when wgpu or the backend rejects it. The
        /// module is cloned because the renderer takes it by value.
        pub fn accepts(&self, name: &str, module: &naga::Module) -> Result<(), String> {
            let scope = self.device.push_error_scope(wgpu::ErrorFilter::Validation);
            let _module = self.device.create_shader_module(wgpu::ShaderModuleDescriptor {
                label: Some(name),
                source: wgpu::ShaderSource::Naga(std::borrow::Cow::Owned(module.clone())),
            });
            match pollster::block_on(scope.pop()) {
                Some(error) => Err(error.to_string()),
                None => Ok(()),
            }
        }
    }
}

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

/// The severity of one program or configuration note.
///
/// The loader records a program it could not load as a `"<name>: skipped"` note and
/// every other note as information (a location the translation could not carry, a
/// setting a later file changed). Only a skip is a critical error: it means the pack
/// provides a program that will not run. Everything else is kept verbatim, but does
/// not fail the run.
fn note_severity(note: &str) -> &'static str {
    if note.ends_with(": skipped") {
        "error"
    } else {
        "info"
    }
}

fn probe_pack(
    pack: &Pack,
    probes: &mut Vec<String>,
    errors: &mut Vec<String>,
    #[cfg(feature = "gpu")] gpu: Option<&gpu::Gpu>,
) -> serde_json::Value {
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

    // Preprocess every stage file of the base and each dimension folder. Keep the
    // full text of every diagnostic and warning, located, keyed by the file that
    // produced it: a diagnostic is a condition the preprocessor could not read, a
    // warning one it read as false.
    let mut stage_files = 0;
    let mut stage_diagnostics: Vec<serde_json::Value> = Vec::new();
    let mut stage_warnings: Vec<serde_json::Value> = Vec::new();
    for dir in ["", "world0", "world-1", "world1"] {
        for file in pack.list(dir) {
            if [".vsh", ".fsh", ".gsh", ".csh"].iter().any(|e| file.ends_with(e)) {
                let path = if dir.is_empty() { file.clone() } else { format!("{dir}/{file}") };
                let preprocessed = horizon_shaders::preprocess(pack, &path, &macros);
                stage_files += 1;
                for (kind, list, sink) in [
                    ("diagnostic", &preprocessed.diagnostics, &mut stage_diagnostics),
                    ("warning", &preprocessed.warnings, &mut stage_warnings),
                ] {
                    for d in list {
                        let origin = preprocessed
                            .files
                            .get(d.origin.file as usize)
                            .cloned()
                            .unwrap_or_else(|| path.clone());
                        sink.push(serde_json::json!({
                            "stage_file": path,
                            "file": origin,
                            "line": d.origin.line,
                            "severity": kind,
                            "message": d.message,
                        }));
                    }
                }
            }
        }
    }
    entry.insert("stage_files".into(), stage_files.into());
    entry.insert("preprocess_diagnostics".into(), stage_diagnostics.len().into());
    entry.insert("preprocess_diagnostics_detail".into(), serde_json::Value::Array(stage_diagnostics.clone()));
    entry.insert("preprocess_warnings".into(), stage_warnings.len().into());
    entry.insert("preprocess_warnings_detail".into(), serde_json::Value::Array(stage_warnings.clone()));

    // The pack's options, screens and profiles, read as the game reads them (8.2, 8.3).
    // `options()` and `Screens::parse` are the loader's own readers; the screen names
    // come from the `screen`/`screen.NAME` keys, which is what `Screens::parse` reads.
    let pack_options = options(pack);
    let screens = Screens::parse(&properties, &pack_options);
    let mut screen_names: Vec<String> = properties
        .entries
        .iter()
        .filter_map(|entry| {
            let key = entry.key.as_str();
            (key == "screen").then(|| "(main)".to_owned()).or_else(|| {
                key.strip_prefix("screen.")
                    .filter(|rest| !rest.ends_with(".columns"))
                    .map(str::to_owned)
            })
        })
        .collect();
    screen_names.sort();
    screen_names.dedup();
    entry.insert("options".into(), serde_json::json!({
        "count": pack_options.len(),
        "names": pack_options.keys().collect::<Vec<_>>(),
    }));
    entry.insert("screens".into(), serde_json::json!({
        "count": screen_names.len(),
        "names": screen_names,
        "sliders": screens.sliders.iter().collect::<Vec<_>>(),
    }));
    entry.insert("profiles".into(), serde_json::json!({
        "count": screens.profiles.len(),
        "names": screens.profiles.iter().map(|(name, _)| name).collect::<Vec<_>>(),
    }));
    // `config_read` is the loader's own readers having produced the pack's settings:
    // `read_properties` (above), `options()` and `Screens::parse` all ran on the pack.
    entry.insert("config_read".into(), serde_json::json!(true));

    // Every dimension's program set, loaded as the game loads it, and lowered.
    let terrain = terrain_inputs();
    let inputs = game_inputs(&terrain);
    let mut programs = 0;
    let mut backend_failures: Vec<String> = Vec::new();
    #[cfg(feature = "gpu")]
    let mut gpu_failures: Vec<String> = Vec::new();
    // Every note the loader recorded while loading the programs, verbatim and
    // classified. `post.notes` says which programs were skipped and why; the config
    // notes say which settings conflicted or were unreadable.
    let mut notes: Vec<serde_json::Value> = Vec::new();
    let mut dimensions = serde_json::Map::new();
    for id in [0, -1, 1] {
        let folder = dimension_folder(pack, id);
        let shaders = Properties::from_pack(pack, &folder, &macros).in_dimension(id);
        let post = load_post(pack, &folder, &shaders, &macros, &inputs);
        let modules = all_modules(&post);
        programs += modules.len();
        for note in &post.notes {
            notes.push(serde_json::json!({
                "dimension": id,
                "source": "post",
                "severity": note_severity(note),
                "note": note,
            }));
        }
        for note in &post.config.notes {
            notes.push(serde_json::json!({
                "dimension": id,
                "source": "config",
                "severity": note_severity(note),
                "note": note,
            }));
        }
        for (name, module) in &modules {
            for backend in [Backend::Msl, Backend::Spv, Backend::Hlsl, Backend::Glsl] {
                lower(name, module, backend, &mut backend_failures);
            }
            // The renderer's own next call: wgpu's `create_shader_module`.
            #[cfg(feature = "gpu")]
            if let Some(gpu) = gpu {
                if let Err(error) = gpu.accepts(name, module) {
                    gpu_failures.push(format!("{name}: {error}"));
                }
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
    entry.insert("post_notes".into(), notes.len().into());
    entry.insert("notes".into(), serde_json::Value::Array(notes.clone()));
    entry.insert("backend_failures".into(), serde_json::json!(backend_failures));
    #[cfg(feature = "gpu")]
    {
        entry.insert("modules_accepted_by_wgpu".into(), serde_json::json!(gpu.is_some()));
        entry.insert("gpu_failures".into(), serde_json::json!(gpu_failures));
    }

    // A pack is clean when no module failed to lower, no module was rejected, and no
    // program was skipped. Preprocess diagnostics are recorded above but are not fatal:
    // a condition the preprocessor read as false is the pack's own behaviour, not a
    // failure of the loader. A skipped program is fatal and is listed in `errors`.
    let skipped: Vec<&serde_json::Value> = notes
        .iter()
        .filter(|n| n.get("severity").and_then(|s| s.as_str()) == Some("error"))
        .collect();
    let applies = !pack.all_files().is_empty();
    #[cfg(not(feature = "gpu"))]
    let clean = backend_failures.is_empty() && skipped.is_empty();
    #[cfg(feature = "gpu")]
    let clean = backend_failures.is_empty() && gpu_failures.is_empty() && skipped.is_empty();
    entry.insert("applies".into(), serde_json::json!(applies && clean));

    if clean {
        #[cfg(feature = "gpu")]
        let suffix = match gpu {
            Some(_) => ", and every module is accepted by wgpu's create_shader_module",
            None => " (no wgpu adapter: module acceptance not checked)",
        };
        #[cfg(not(feature = "gpu"))]
        let suffix = "";
        probes.push(format!(
            "{}: {} stage files, {programs} modules lower to MSL/SPV/HLSL/GLSL{suffix}",
            pack.name(),
            stage_files
        ));
    } else {
        if !skipped.is_empty() {
            errors.push(format!(
                "{}: {} program(s) skipped; first: {}",
                pack.name(),
                skipped.len(),
                skipped[0].get("note").and_then(|n| n.as_str()).unwrap_or("")
            ));
        }
        #[allow(unused_mut)]
        let mut detail: Vec<String> = backend_failures.clone();
        #[cfg(feature = "gpu")]
        detail.extend(gpu_failures.iter().cloned());
        if !detail.is_empty() {
            errors.push(format!(
                "{}: {} lowering/acceptance failure(s) across {} modules; first: {}",
                pack.name(),
                detail.len(),
                programs,
                detail.first().cloned().unwrap_or_default()
            ));
        }
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
        } else if arg == "--require-backend" {
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
    // `--require-backend metal` makes a run prove the modules were accepted by a
    // specific backend (a real GPU), not just by whichever adapter came first.
    let required_backend = args
        .iter()
        .position(|a| a == "--require-backend")
        .and_then(|i| args.get(i + 1))
        .cloned();
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

    // With the `gpu` feature, open a headless wgpu device once (a software adapter
    // is enough) so every module also goes through the renderer's own
    // `create_shader_module` call. Without an adapter the run still reports
    // lowering, and says module acceptance was not checked.
    #[cfg(feature = "gpu")]
    let gpu = match gpu::Gpu::open(required_backend.as_deref()) {
        Ok(gpu) => {
            eprintln!("wgpu adapter: {}", gpu.adapter);
            Some(gpu)
        }
        Err(reason) => {
            eprintln!("note: {reason}; module acceptance by wgpu will not be checked");
            if required_backend.is_some() {
                // A required backend that could not be opened is a hard failure: the
                // caller asked to prove a specific backend accepted the modules.
                eprintln!("error: required backend was not available: {reason}");
                return ExitCode::from(3);
            }
            None
        }
    };

    for path in &paths {
        match Pack::open(path) {
            Ok(pack) => packs.push(probe_pack(
                &pack,
                &mut probes,
                &mut errors,
                #[cfg(feature = "gpu")]
                gpu.as_ref(),
            )),
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

    // Aggregate every diagnostic and note across packs so the report carries the
    // totals without the reader having to walk each pack. `errors` counts notes that
    // are fatal (a skipped program); warnings are the preprocessor's own warnings and
    // the loader's informational notes, kept verbatim in each pack's `notes`.
    let mut diag_errors = 0u64;
    let mut diag_warnings = 0u64;
    let mut diag_total = 0u64;
    for pack in &packs {
        if let Some(list) = pack.get("notes").and_then(|n| n.as_array()) {
            for note in list {
                diag_total += 1;
                match note.get("severity").and_then(|s| s.as_str()) {
                    Some("error") => diag_errors += 1,
                    _ => diag_warnings += 1,
                }
            }
        }
        for key in ["preprocess_diagnostics", "preprocess_warnings"] {
            if let Some(count) = pack.get(key).and_then(|c| c.as_u64()) {
                diag_total += count;
                if key == "preprocess_diagnostics" {
                    diag_errors += count;
                } else {
                    diag_warnings += count;
                }
            }
        }
    }

    #[allow(unused_mut)]
    let mut report = serde_json::json!({
        "packs": packs,
        "lowered": probes,
        "failures": errors,
        "diagnostics": {
            "total": diag_total,
            "errors": diag_errors,
            "warnings": diag_warnings,
        },
        "severity_rules": "error: a program the loader skipped (its note ends \": skipped\") or a \
                           preprocess diagnostic (a condition the preprocessor could not read). \
                           info/warning: every other note, kept verbatim; these do not fail the run.",
        "caveat": "MSL/SPIR-V/HLSL/GLSL lowering proves the loader can hand a translated \
                   module to the renderer's shader writer, and (with the `gpu` feature) \
                   wgpu's `create_shader_module` accepts it on a software adapter. Neither \
                   proves the pack renders as intended on a device: that needs the real \
                   Metal backend on a physical iPad, which this headless probe does not have.",
        "ok": errors.is_empty(),
    });
    #[cfg(feature = "gpu")]
    if let Some(map) = report.as_object_mut() {
        map.insert("wgpu_adapter".into(), serde_json::json!(gpu.as_ref().map(|g| g.adapter.clone())));
        map.insert("wgpu_backend".into(), serde_json::json!(gpu.as_ref().map(|g| g.backend.clone())));
        map.insert("modules_accepted_by_wgpu".into(), serde_json::json!(gpu.is_some()));
        map.insert("required_backend".into(), serde_json::json!(required_backend.clone()));
    }
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
