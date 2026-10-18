#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn damaged_model_projections_rebuild_without_following_foreign_targets() {
    for damage_parent in [false, true] {
        let data = tempdir().unwrap();
        let plan: CompiledExecutionPlan = serde_json::from_value(compiled_plan()).unwrap();
        let objects = data.path().join("distribution/models");
        fs::create_dir_all(&objects).unwrap();
        for (artifact, bytes) in [
            (&plan.artifacts[0], b"primary".as_slice()),
            (&plan.artifacts[1], b"secondary".as_slice()),
        ] {
            let source = objects.join(&artifact.sha256);
            fs::write(&source, bytes).unwrap();
            fs::set_permissions(source, fs::Permissions::from_mode(0o600)).unwrap();
        }
        let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f";
        let paths = materialize_compiled_models(data.path(), &plan, installation_id).unwrap();
        let foreign = data.path().join("foreign");
        fs::create_dir(&foreign).unwrap();
        fs::write(foreign.join("config.json"), b"foreign bytes").unwrap();
        if damage_parent {
            fs::remove_file(&paths[0]).unwrap();
            fs::remove_dir(paths[0].parent().unwrap()).unwrap();
            symlink(&foreign, paths[0].parent().unwrap()).unwrap();
        } else {
            fs::remove_file(&paths[0]).unwrap();
            symlink(foreign.join("config.json"), &paths[0]).unwrap();
        }
        materialize_compiled_models(data.path(), &plan, installation_id).unwrap();
        assert_eq!(fs::read(&paths[0]).unwrap(), b"primary");
        assert_eq!(
            fs::read(foreign.join("config.json")).unwrap(),
            b"foreign bytes"
        );
        let inode = fs::metadata(&paths[0]).unwrap().ino();
        materialize_compiled_models(data.path(), &plan, installation_id).unwrap();
        assert_eq!(fs::metadata(&paths[0]).unwrap().ino(), inode);
    }
}
