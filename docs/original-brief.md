## Agent-Playground

This is a set of skills to build out an agent-playground, a place for you to test agents in a repeatable manner.

When working on agents (or harnesses) we need to be able to duplicate APIs and MCPs in such a way that you can test/evaluate consistently.

### Steps
1. When given an API or MCP you look at the swagger page or MCP documentation one endpoint at a time
2. You store samples of the data in a local sqllite DB
3. Your record how various calls return data, the shape and type of data - ideally it's all documented, but sometimes it isn't (for example if the expected return in random json)
4. You build out stubs for each of the endpoints, that have the exact same API/MCP signature
5. You give it the ability to return the sampled data - and VERY IMPORTANT make sure the data is consistent across the various endpoints
For example, if you have an API with 2 endpoints
GET tickers and GET exchanges, you need to make sure that each ticker has a valid exchange in the exchanges endpoint
or
POST upload and GET upload_status, if you uploaded a doc, it should reflect in the upload_status endpoint correctly - make sure to take timing into account and the various states that occur!
6. A thorough review of all the endpoints where you write a lot of end-to-end tests that you can run on both the real and stubbed endpoints to make sure they are the same

### Complexity
This is not supposed to be a fast process, I expect that even given a simple endpoint this could take hours to run in Claude or another framework - that's fine! It's far better to be thorough rather than incomplete!

You need to take timing into account, for instance if you have an endpoint
GET current_date_time - it should always return the actual datetime, not a frozen time from when the script ran

In a more complex case, if you have an endpoint
GET ticker_price, you can't keep updating the price, but you can update the time field that's sure to come along with it and this case is interesting, because during market hours it should acr one way (the time should be relatively up to date) but overnight that time will be frozen at the last tick.

These are all just examples to emphasize how in-depth this skill needs to go in order for this process to work.

### Code Architecture
Each endpoint should spin up a lightweight uvicorn/fastapi server, using pydantic_settings to define configuration and have a consistent logging framework (define one for me).

It should use pydantic as much as possible
It should use UV
It should use mypy for typesafety
It should use ruff for linting
It should have at least 95% code coverage via unit tests

Each fake mcp/api should have enough end to end tests to ensure 98% or better compliance with the actual endpoints.

As much of this architecture that can be stubbed out in the skill do so.

If you need a service to make probabilistic choices, use Jev - the user will be expected to add his/her token to the config file.

### Calling
/agent-playground <swagger_page>
or
/agent-playground <api-documentation-page>