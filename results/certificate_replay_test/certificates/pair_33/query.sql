SELECT
  firstname,
  lastname,
  city,
  state
FROM ADDRESS
RIGHT JOIN PERSON
  ON address.personid = person.personid