"""The agent from Google's page "Create a LangGraph agent", with ITS checkpointer recipe, nothing else:
a model and no tools (the prebuilt agent is then a single model node) plus checkpointer_builder -> Cloud SQL PostgresSaver.
https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/runtime/create-a-langgraph-agent"""
from vertexai import agent_engines

MODEL = "gemini-2.5-flash"  # the page uses a newer name; this one is known to work in this project


def checkpointer_builder(**kwargs):
    # imports INSIDE the function, as on the page: the agent object is pickled and rebuilt in the runtime
    from langchain_google_cloud_sql_pg import PostgresEngine, PostgresSaver

    engine = PostgresEngine.from_instance(**kwargs)
    engine.init_checkpoint_table()
    return PostgresSaver.create_sync(engine)


def make_agent(checkpointer_kwargs: dict):
    return agent_engines.LanggraphAgent(
        model=MODEL,
        checkpointer_kwargs=checkpointer_kwargs,
        checkpointer_builder=checkpointer_builder,
    )
